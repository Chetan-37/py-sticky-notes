#!/usr/bin/env python3
"""
PyStickyNotes - a lightweight Windows-style Sticky Notes clone built with PyQt5.

Key features
------------
* Multiple frameless, draggable note windows with a soft drop shadow.
* Resize from ANY edge or corner (plus a size grip at the bottom-right).
* 7 pastel colour themes (header bar + body colour) switchable per note.
* "Pin" a note to keep it always on top of other windows.
* Rich-text formatting: bold, italic, underline, strikethrough, bullet lists.
* Adjustable font size per note (A-/A+ buttons, Ctrl +/-, Ctrl + mouse wheel).
* "List" window to search, re-open or delete every note, including closed ones.
* Auto-save to ~/.config/py_sticky_notes.json (debounced + atomic write) and
  full restore of text, formatting, colour, size, position and pin state.

Design notes
------------
* StickyNoteManager  - owns the QApplication, the list of notes and all disk I/O.
* StickyNote         - one note window (UI, formatting, drag/resize logic).
* NoteEdit           - QTextEdit subclass with smart bullet-list behaviour.
* NotesListWindow    - searchable overview of all notes.

Requirements: Python 3.8+, PyQt5   (pip install PyQt5)
Run:          python3 sticky_notes.py
"""
import sys
import json
import os
import uuid
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QFrame, QGraphicsDropShadowEffect,
    QListWidget, QListWidgetItem, QLineEdit, QLabel, QMessageBox,
    QSizeGrip, QShortcut
)
from PyQt5.QtCore import Qt, QPoint, QSize, QTimer, QEvent, QRect, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QTextListFormat, QKeySequence, QTextCursor, QTextCharFormat

# All notes are stored in a single JSON file (a list of note dictionaries).
NOTES_FILE = os.path.expanduser("~/.config/py_sticky_notes.json")

# Balanced, soft pastel Windows-style themes.
# bg = note body, header = top bar, text = font colour, accent = swatch border.
THEMES = {
    "Yellow":   {"bg": "#FEF3A4", "header": "#FCEB82", "text": "#2C2C2C", "accent": "#DFC848"},
    "Green":    {"bg": "#D5EDD8", "header": "#C0E4C3", "text": "#2C2C2C", "accent": "#8FB993"},
    "Pink":     {"bg": "#FCE1E8", "header": "#F9CCD7", "text": "#2C2C2C", "accent": "#D693A3"},
    "Purple":   {"bg": "#EFE0F5", "header": "#E4CEEF", "text": "#2C2C2C", "accent": "#B493C8"},
    "Blue":     {"bg": "#D6EFFE", "header": "#BDE2FE", "text": "#2C2C2C", "accent": "#8BBBE2"},
    "Charcoal": {"bg": "#32363D", "header": "#25282E", "text": "#F0F0F0", "accent": "#5C6370"},
    "White":    {"bg": "#F8F8F8", "header": "#EDEDED", "text": "#2C2C2C", "accent": "#C8C8C8"},
}


def remove_block_from_list(block):
    """Take a block out of its bullet list and reset its indent."""
    lst = block.textList()
    if lst is None:
        return
    lst.remove(block)
    cur = QTextCursor(block)
    fmt = cur.blockFormat()
    fmt.setIndent(0)
    cur.setBlockFormat(fmt)


class NoteEdit(QTextEdit):
    """QTextEdit with proper bullet-list behaviour, plain-text paste and
    Ctrl+wheel zoom that goes through the note's own font-size logic.

    Extras over a plain QTextEdit:
      * the bullet disappears when all text is cleared,
      * Enter on an empty bullet / Backspace at a bullet's start ends the list,
      * the placeholder is hidden while the first line is a bullet,
      * pasting always inserts plain text (keeps fonts/colours consistent).
    """
    zoomRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._was_empty = True
        self._fixing = False
        self._placeholder = "Take a note..."
        self.setPlaceholderText(self._placeholder)
        self.textChanged.connect(self._drop_bullet_when_emptied)
        self.textChanged.connect(self.sync_placeholder)
        self.cursorPositionChanged.connect(self.sync_placeholder)

    def sync_placeholder(self):
        # The placeholder is painted at the left margin and overlaps an empty
        # bullet, so hide it while the (empty) first line is a bullet.
        in_list = self.document().firstBlock().textList() is not None
        self.setPlaceholderText("" if in_list else self._placeholder)

    def reset_empty_state(self):
        self._was_empty = self.document().isEmpty()

    def _drop_bullet_when_emptied(self):
        # Bug fix: clearing all text (select-all + delete, cut, undo...) used to
        # leave an empty bullet behind. When the document *becomes* empty,
        # remove the list from the remaining block.
        if self._fixing:
            return
        empty = self.document().isEmpty()
        if empty and not self._was_empty:
            block = self.document().firstBlock()
            if block.textList() is not None:
                self._fixing = True
                try:
                    cur = QTextCursor(block)
                    cur.joinPreviousEditBlock()
                    remove_block_from_list(block)
                    cur.endEditBlock()
                finally:
                    self._fixing = False
        self._was_empty = empty

    def keyPressEvent(self, e):
        cursor = self.textCursor()
        mods = e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
        if cursor.currentList() is not None and not cursor.hasSelection() and not mods:
            key = e.key()
            block = cursor.block()
            # Enter on an empty bullet, or Backspace at the start of a bullet,
            # ends the list (Word / Sticky Notes style).
            if key in (Qt.Key_Return, Qt.Key_Enter) and block.text() == "":
                remove_block_from_list(block)
                return
            if key == Qt.Key_Backspace and cursor.positionInBlock() == 0:
                remove_block_from_list(block)
                return
        super().keyPressEvent(e)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoomRequested.emit(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
            return
        super().wheelEvent(e)

    def insertFromMimeData(self, source):
        # Pasted web/Word content would bring its own fonts, sizes and colours
        # (e.g. dark text on the Charcoal theme). Paste as plain text.
        if source.hasText():
            self.insertPlainText(source.text())
        else:
            super().insertFromMimeData(source)


class StickyNote(QWidget):
    """A single frameless sticky-note window.

    Layout (top to bottom): header bar (new / list / colour / pin / delete /
    close) -> optional colour picker -> text editor -> formatting toolbar.
    The window itself is transparent; a rounded 'card' frame with a drop shadow
    is drawn inside it, and the transparent border doubles as the resize handle.
    """

    def __init__(self, data=None, manager=None):
        super().__init__()
        self.manager = manager
        self.old_pos = None
        self._applying_size = False
        self._resize_edges = ()      # which edges are being dragged
        self._resize_start = None    # (global point, geometry) at press

        self.note_id = data.get("id", str(uuid.uuid4())) if data else str(uuid.uuid4())
        self.is_pinned = data.get("pinned", False) if data else False
        self.current_theme = data.get("theme", "Yellow") if data else "Yellow"
        self.is_open = data.get("is_open", True) if data else True
        self.font_size = data.get("font_size", 14) if data else 14

        self.init_ui(data)
        self.init_shortcuts()

    def init_shortcuts(self):
        # Bug fix: "Ctrl++"/"Ctrl+Plus" and "Ctrl+-"/"Ctrl+Minus" are the SAME key
        # sequence, so Qt saw ambiguous shortcuts and fired none of them.
        for seq in ("Ctrl++", "Ctrl+="):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(self.increase_font_size)

        sc = QShortcut(QKeySequence("Ctrl+-"), self)
        sc.activated.connect(self.decrease_font_size)
		
		# Text formatting shortcuts
        QShortcut(QKeySequence("Ctrl+B"), self).activated.connect(self.fmt_bold)
        QShortcut(QKeySequence("Ctrl+I"), self).activated.connect(self.fmt_italic)
        QShortcut(QKeySequence("Ctrl+U"), self).activated.connect(self.fmt_underline)
        QShortcut(QKeySequence("Ctrl+Shift+X"), self).activated.connect(self.fmt_strikethrough)
   
    def init_ui(self, data):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        if self.is_pinned:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(240, 200)
        self.setMouseTracking(True)   # needed for edge-resize hover cursors

        geometry = data.get("geometry", [200, 200, 280, 320]) if data else [200, 200, 280, 320]
        self.setGeometry(*geometry)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(self.RESIZE_MARGIN, self.RESIZE_MARGIN, self.RESIZE_MARGIN, self.RESIZE_MARGIN)

        self.card = QFrame(self)
        self.card.setObjectName("Card")

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(16)
        shadow.setColor(QColor(0, 0, 0, 50))
        shadow.setYOffset(3)
        shadow.setXOffset(0)
        self.card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # --- HEADER BAR ---
        self.header = QFrame(self.card)
        self.header.setObjectName("Header")
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(6, 4, 6, 4)
        header_layout.setSpacing(4)

        self.btn_add = QPushButton("+")
        self.btn_add.setToolTip("New Note")
        self.btn_add.clicked.connect(lambda: self.manager.create_note())

        self.btn_list = QPushButton("List")
        self.btn_list.setToolTip("Notes List")
        self.btn_list.clicked.connect(self.manager.show_list_window)

        self.btn_palette = QPushButton("Color")
        self.btn_palette.setToolTip("Change Theme")
        self.btn_palette.clicked.connect(self.toggle_color_bar)

        self.btn_pin = QPushButton("Pinned" if self.is_pinned else "Pin")
        self.btn_pin.setToolTip("Toggle Always on Top")
        self.btn_pin.clicked.connect(self.toggle_pin)

        self.btn_delete = QPushButton("Del")
        self.btn_delete.setToolTip("Delete Note Permanently")
        self.btn_delete.clicked.connect(self.delete_note_permanently)

        self.btn_close = QPushButton("✕")
        self.btn_close.setToolTip("Close Window (Saved in List)")
        self.btn_close.clicked.connect(self.close_window)

        header_layout.addWidget(self.btn_add)
        header_layout.addWidget(self.btn_list)
        header_layout.addWidget(self.btn_palette)
        header_layout.addWidget(self.btn_pin)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_delete)
        header_layout.addWidget(self.btn_close)

        # --- COLOR SELECTION BAR ---
        self.color_bar = QFrame(self.card)
        self.color_bar.setVisible(False)
        color_layout = QHBoxLayout(self.color_bar)
        color_layout.setContentsMargins(8, 4, 8, 6)
        color_layout.setSpacing(6)

        for name, colors in THEMES.items():
            btn = QPushButton()
            btn.setFixedSize(18, 18)
            btn.setToolTip(name)
            btn.setStyleSheet(f"background-color: {colors['bg']}; border: 1px solid {colors['accent']}; border-radius: 9px;")
            btn.clicked.connect(lambda checked, t=name: self.set_theme(t))
            color_layout.addWidget(btn)

        # --- TEXT EDITOR ---
        self.text_edit = NoteEdit(self.card)

        if data and "html" in data and data["html"]:
            self.text_edit.setHtml(data["html"])
        elif data and "text" in data:
            self.text_edit.setPlainText(data["text"])
        self.text_edit.reset_empty_state()

        self.apply_font_size_to_document()

        self.text_edit.textChanged.connect(self.on_content_changed)
        self.text_edit.currentCharFormatChanged.connect(self.update_format_buttons)
        self.text_edit.cursorPositionChanged.connect(self.update_format_buttons)
        self.text_edit.zoomRequested.connect(self.on_zoom_requested)

        # --- BOTTOM FORMATTING TOOLBAR ---
        self.toolbar = QFrame(self.card)
        toolbar_layout = QHBoxLayout(self.toolbar)
        toolbar_layout.setContentsMargins(6, 2, 4, 4)
        toolbar_layout.setSpacing(2)

        self.btn_bold = QPushButton("B")
        self.btn_bold.setFont(QFont("Sans", 9, QFont.Bold))
        self.btn_bold.setCheckable(True)
        self.btn_bold.clicked.connect(self.fmt_bold)

        self.btn_italic = QPushButton("I")
        self.btn_italic.setFont(QFont("Sans", 9, QFont.Normal, True))
        self.btn_italic.setCheckable(True)
        self.btn_italic.clicked.connect(self.fmt_italic)

        self.btn_underline = QPushButton("U")
        self.btn_underline.setCheckable(True)
        self.btn_underline.clicked.connect(self.fmt_underline)

        self.btn_strike = QPushButton("S")
        self.btn_strike.setCheckable(True)
        self.btn_strike.clicked.connect(self.fmt_strikethrough)

        self.btn_font_dec = QPushButton("A-")
        self.btn_font_dec.setToolTip("Decrease Font Size (Ctrl + -)")
        self.btn_font_dec.clicked.connect(self.decrease_font_size)

        self.btn_font_inc = QPushButton("A+")
        self.btn_font_inc.setToolTip("Increase Font Size (Ctrl + +)")
        self.btn_font_inc.clicked.connect(self.increase_font_size)

        self.btn_bullet = QPushButton("• List")
        self.btn_bullet.setToolTip("Toggle bullet list")
        self.btn_bullet.clicked.connect(self.fmt_bullet)

        for btn in (self.btn_bold, self.btn_italic, self.btn_underline, self.btn_strike, self.btn_font_dec, self.btn_font_inc):
            btn.setFixedSize(24, 22)
            toolbar_layout.addWidget(btn)

        self.btn_bullet.setFixedSize(46, 22)
        toolbar_layout.addWidget(self.btn_bullet)

        toolbar_layout.addStretch()

        # Resizable Grip
        self.size_grip = QSizeGrip(self.card)
        self.size_grip.setFixedSize(14, 14)
        toolbar_layout.addWidget(self.size_grip, 0, Qt.AlignBottom | Qt.AlignRight)

        card_layout.addWidget(self.header)
        card_layout.addWidget(self.color_bar)
        card_layout.addWidget(self.text_edit, 1)
        card_layout.addWidget(self.toolbar)

        outer_layout.addWidget(self.card)
        # reset the resize cursor as soon as the mouse moves from the border onto the card
        self.card.installEventFilter(self)

        # Buttons must not steal keyboard focus from the editor, otherwise the
        # cursor disappears / typing stops after clicking B, I, U, A+, etc.
        for b in self.card.findChildren(QPushButton):
            b.setFocusPolicy(Qt.NoFocus)

        self.apply_theme()
        self.update_format_buttons()

    # ---------- font size ----------
    def apply_font_size_to_document(self):
        """Apply self.font_size to the editor, the existing text, the empty
        block (so the caret is the right size) and future typing."""
        te = self.text_edit
        doc = te.document()

        font = QFont(te.font())
        font.setFamily("Segoe UI")
        font.setPointSize(self.font_size)
        te.setFont(font)
        doc.setDefaultFont(font)

        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(self.font_size))

        # Use a separate cursor so the user's cursor/selection isn't disturbed.
        # Setting the *block* char format is what fixes the empty-note case: the
        # caret height of an empty block comes from the block's char format, and
        # the old code only changed the text's format (nothing, when empty).
        # NOTE: do NOT block the document's signals here - the text layout listens
        # to them, and without them the layout/caret stay stale (invisible caret,
        # wrong bullet size) until the next keystroke.
        self._applying_size = True
        try:
            cur = QTextCursor(doc)
            cur.beginEditBlock()
            cur.select(QTextCursor.Document)
            cur.mergeCharFormat(fmt)
            cur.mergeBlockCharFormat(fmt)
            cur.endEditBlock()
        finally:
            self._applying_size = False

        # Also update what the user's own cursor will type with
        te.mergeCurrentCharFormat(fmt)

        # An empty block's layout is NOT recomputed by a format-only change, so
        # the caret kept its old (tiny/invisible) size. Force a relayout.
        doc.markContentsDirty(0, max(1, doc.characterCount()))
        te.setTextCursor(te.textCursor())   # re-evaluate caret rect
        te.ensureCursorVisible()
        te.viewport().update()

    def change_font_size(self, delta):
        """Change size by `delta` points, clamped to 10-36, and save."""
        new_size = max(10, min(36, self.font_size + delta))
        if new_size == self.font_size:
            return
        self.font_size = new_size
        self.apply_font_size_to_document()
        self.manager.save_notes()

    def increase_font_size(self):
        self.change_font_size(2)

    def decrease_font_size(self):
        self.change_font_size(-2)

    def on_zoom_requested(self, direction):
        self.change_font_size(2 * direction)

    def on_content_changed(self):
        if self._applying_size:
            return
        self.manager.save_notes()
        self.manager.refresh_list_window()

    def update_format_buttons(self, fmt=None):
        if fmt is None:
            fmt = self.text_edit.currentCharFormat()

        self.btn_bold.setChecked(fmt.fontWeight() >= QFont.DemiBold)
        self.btn_italic.setChecked(fmt.fontItalic())
        self.btn_underline.setChecked(fmt.fontUnderline())
        self.btn_strike.setChecked(fmt.fontStrikeOut())

    # ---------- theme ----------
    def apply_theme(self):
        """Re-style the whole card (body, header, editor, buttons) from THEMES."""
        theme = THEMES.get(self.current_theme, THEMES["Yellow"])
        # Bug fix: theme["header"] was defined but never used.
        self.card.setStyleSheet(f"""
            QFrame#Card {{
                background-color: {theme['bg']};
                border: 1px solid rgba(0, 0, 0, 0.12);
                border-radius: 8px;
            }}
            QFrame#Header {{
                background-color: {theme['header']};
                border: none;
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
            }}
            QTextEdit {{
                background-color: transparent;
                color: {theme['text']};
                border: none;
                padding: 6px 10px;
            }}
            QPushButton {{
                background-color: transparent;
                color: {theme['text']};
                border: 1px solid transparent;
                border-radius: 4px;
                font-weight: 600;
                font-size: 11px;
                padding: 2px 2px;
            }}
            QPushButton:hover {{
                background-color: rgba(0, 0, 0, 0.08);
            }}
            QPushButton:checked {{
                background-color: rgba(0, 0, 0, 0.18);
                border: 1px solid rgba(0, 0, 0, 0.2);
                font-weight: bold;
            }}
        """)

        for btn in (self.btn_add, self.btn_list, self.btn_palette, self.btn_pin, self.btn_delete, self.btn_close):
            btn.setFixedHeight(22)

    def set_theme(self, theme_name):
        self.current_theme = theme_name
        self.apply_theme()
        self.color_bar.setVisible(False)
        self.manager.save_notes()
        self.manager.refresh_list_window()

    def toggle_color_bar(self):
        self.color_bar.setVisible(not self.color_bar.isVisible())

    def toggle_pin(self):
        self.is_pinned = not self.is_pinned
        geo = self.geometry()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.is_pinned)
        self.setGeometry(geo)
        self.btn_pin.setText("Pinned" if self.is_pinned else "Pin")
        self.show()
        self.manager.save_notes()

    # ---------- formatting ----------
    # Each helper toggles its attribute on the selection, or on the text that
    # will be typed next when nothing is selected.
    def fmt_bold(self):
        is_bold = self.text_edit.currentCharFormat().fontWeight() >= QFont.DemiBold
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Normal if is_bold else QFont.Bold)
        self.text_edit.mergeCurrentCharFormat(fmt)
        self.update_format_buttons()

    def fmt_italic(self):
        self.text_edit.setFontItalic(not self.text_edit.fontItalic())
        self.update_format_buttons()

    def fmt_underline(self):
        self.text_edit.setFontUnderline(not self.text_edit.fontUnderline())
        self.update_format_buttons()

    def fmt_strikethrough(self):
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(not self.text_edit.currentCharFormat().fontStrikeOut())
        self.text_edit.mergeCurrentCharFormat(fmt)
        self.update_format_buttons()

    def fmt_bullet(self):
        """Toggle a bullet list on every line touched by the selection."""
        # Bug fix: the old code used cursor.insertList(), which inserts a NEW
        # block (splitting the current line) and could never turn bullets off.
        # Now it toggles the list on every block touched by the selection.
        te = self.text_edit
        doc = te.document()
        cursor = te.textCursor()

        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        if end > start:
            end -= 1  # selection ending at a line start shouldn't include that line
        first = doc.findBlock(start)
        last = doc.findBlock(end)

        blocks = []
        b = first
        while b.isValid():
            blocks.append(b)
            if b.blockNumber() >= last.blockNumber():
                break
            b = b.next()

        cursor.beginEditBlock()
        if all(blk.textList() is not None for blk in blocks):
            for blk in blocks:
                remove_block_from_list(blk)
        else:
            lst = None
            for blk in blocks:
                if lst is None:
                    lst = QTextCursor(blk).createList(QTextListFormat.ListDisc)
                else:
                    lst.add(blk)
        cursor.endEditBlock()
        te.sync_placeholder()
        te.setFocus()

    # ---------- window dragging + edge/corner resizing ----------
    RESIZE_MARGIN = 8   # width of the transparent border that acts as resize handle

    def _edges_at(self, pos):
        """Return which window edges ('left','right','top','bottom') the
        point is touching; two entries means a corner."""
        m = self.RESIZE_MARGIN
        edges = []
        if pos.x() <= m:
            edges.append("left")
        elif pos.x() >= self.width() - m:
            edges.append("right")
        if pos.y() <= m:
            edges.append("top")
        elif pos.y() >= self.height() - m:
            edges.append("bottom")
        return tuple(edges)

    @staticmethod
    def _cursor_for(edges):
        e = set(edges)
        if e in ({"left", "top"}, {"right", "bottom"}):
            return Qt.SizeFDiagCursor
        if e in ({"right", "top"}, {"left", "bottom"}):
            return Qt.SizeBDiagCursor
        if e & {"left", "right"}:
            return Qt.SizeHorCursor
        return Qt.SizeVerCursor

    def eventFilter(self, obj, event):
        if obj is self.card and event.type() == QEvent.Enter and not self._resize_edges:
            self.unsetCursor()
        return super().eventFilter(obj, event)

    @staticmethod
    def _global_point(event):
        pos = event.globalPosition() if hasattr(event, 'globalPosition') else event.globalPos()
        return pos.toPoint() if hasattr(pos, 'toPoint') else pos

    # Mouse handling: pressing on the transparent border starts a resize,
    # pressing anywhere else on the note starts a window drag.
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.pos() if hasattr(event, 'pos') else event.position().toPoint()
        edges = self._edges_at(pos)
        gp = self._global_point(event)
        if edges:
            self._resize_edges = edges
            self._resize_start = (gp, self.geometry())
        else:
            self.old_pos = gp

    def mouseMoveEvent(self, event):
        gp = self._global_point(event)

        if self._resize_edges:
            start_pt, geo = self._resize_start
            dx, dy = gp.x() - start_pt.x(), gp.y() - start_pt.y()
            left, top, right, bottom = geo.left(), geo.top(), geo.right(), geo.bottom()
            min_w, min_h = self.minimumWidth(), self.minimumHeight()
            if "left" in self._resize_edges:
                left = min(left + dx, right - min_w + 1)
            if "right" in self._resize_edges:
                right = max(right + dx, left + min_w - 1)
            if "top" in self._resize_edges:
                top = min(top + dy, bottom - min_h + 1)
            if "bottom" in self._resize_edges:
                bottom = max(bottom + dy, top + min_h - 1)
            self.setGeometry(QRect(QPoint(left, top), QPoint(right, bottom)))
            return

        if self.old_pos is not None:
            delta = QPoint(gp - self.old_pos)
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = gp
            return

        # hover (no button): show a resize cursor over the border
        pos = event.pos() if hasattr(event, 'pos') else event.position().toPoint()
        edges = self._edges_at(pos)
        if edges:
            self.setCursor(self._cursor_for(edges))
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event):
        if self._resize_edges or self.old_pos is not None:
            self._resize_edges = ()
            self._resize_start = None
            self.old_pos = None
            self.unsetCursor()
            self.manager.save_notes()

    def leaveEvent(self, event):
        if not self._resize_edges:
            self.unsetCursor()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.manager and not self.manager.is_loading:
            self.manager.save_notes()

    # ---------- close / delete ----------
    def close_window(self):
        """Hide the note (it stays saved and can be re-opened from the list)."""
        self.is_open = False
        # Bug fix: hiding the last visible window quits the whole app. Show the
        # list window first so there is always something on screen.
        if not any(n.is_open for n in self.manager.notes):
            self.manager.show_list_window()
        self.hide()
        self.manager.save_notes()
        self.manager.refresh_list_window()

    def delete_note_permanently(self):
        reply = QMessageBox.question(
            self, 'Delete Note', 'Are you sure you want to delete this note permanently?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.manager.remove_note(self)

    def to_dict(self):
        """Serialise everything needed to restore this note later."""
        geo = self.geometry()
        return {
            "id": self.note_id,
            "html": self.text_edit.toHtml(),
            "text": self.text_edit.toPlainText(),
            "theme": self.current_theme,
            "pinned": self.is_pinned,
            "is_open": self.is_open,
            "font_size": self.font_size,
            "geometry": [geo.x(), geo.y(), geo.width(), geo.height()]
        }


# --- ALL NOTES LIST WINDOW ---
class NotesListWindow(QWidget):
    """Overview of every note (open or closed) with live search, plus
    Open / Delete buttons per row and a '+ New Note' button."""

    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.setWindowTitle("Sticky Notes - All Notes")
        self.resize(340, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        header_layout = QHBoxLayout()
        title_label = QLabel("<b>Sticky Notes</b>")
        title_label.setStyleSheet("font-size: 16px; color: #1F2937;")

        btn_add = QPushButton("+ New Note")
        btn_add.setStyleSheet("background-color: #2563EB; color: white; border-radius: 4px; padding: 5px 10px; font-weight: bold;")
        btn_add.clicked.connect(lambda: self.manager.create_note())

        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(btn_add)
        layout.addLayout(header_layout)

        # Search Bar
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search notes...")
        self.search_box.setStyleSheet("padding: 6px; border: 1px solid #D1D5DB; border-radius: 6px;")
        self.search_box.textChanged.connect(self.populate_list)
        layout.addWidget(self.search_box)

        # List Widget
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget { border: 1px solid #E5E7EB; border-radius: 6px; padding: 4px; }
            QListWidget::item { border-bottom: 1px solid #E5E7EB; margin-bottom: 4px; border-radius: 4px; }
            QListWidget::item:hover { background-color: #F3F4F6; }
        """)
        layout.addWidget(self.list_widget)

    def populate_list(self, *_):
        self.list_widget.clear()
        query = self.search_box.text().lower()

        for note in self.manager.notes:
            text = note.text_edit.toPlainText().strip()
            preview = text if text else "(Empty Note)"

            if query and query not in preview.lower():
                continue

            first_line = preview.split("\n")[0][:26]

            item_widget = QWidget()
            theme = THEMES.get(note.current_theme, THEMES["Yellow"])
            item_widget.setStyleSheet(f"background-color: {theme['bg']}; border-radius: 4px;")

            item_layout = QHBoxLayout(item_widget)
            item_layout.setContentsMargins(8, 6, 8, 6)

            lbl_text = QLabel(first_line)
            lbl_text.setStyleSheet(f"color: {theme['text']}; font-weight: bold; font-size: 13px;")

            btn_del = QPushButton("Delete")
            btn_del.setStyleSheet("""
                QPushButton {
                    background-color: rgba(220, 38, 38, 0.85);
                    color: white;
                    border: none;
                    border-radius: 3px;
                    padding: 3px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: rgba(185, 28, 28, 1);
                }
            """)
            btn_del.clicked.connect(lambda checked, n=note: self.delete_note_from_list(n))

            btn_open = QPushButton("Open")
            btn_open.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(0, 0, 0, 0.08);
                    color: {theme['text']};
                    border: none;
                    border-radius: 3px;
                    padding: 3px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: rgba(0, 0, 0, 0.15);
                }}
            """)
            btn_open.clicked.connect(lambda checked, n=note: self.open_note(n))

            item_layout.addWidget(lbl_text, 1)
            item_layout.addWidget(btn_open)
            item_layout.addWidget(btn_del)

            list_item = QListWidgetItem(self.list_widget)
            list_item.setSizeHint(QSize(0, 42))
            self.list_widget.addItem(list_item)
            self.list_widget.setItemWidget(list_item, item_widget)

    def open_note(self, note):
        note.is_open = True
        note.show()
        note.raise_()
        note.activateWindow()
        self.manager.save_notes()
        self.manager.refresh_list_window()

    def delete_note_from_list(self, note):
        reply = QMessageBox.question(
            self, 'Delete Note', 'Delete this note permanently?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.manager.remove_note(note)


class StickyNoteManager:
    """Application controller: creates/removes notes, loads and saves the JSON
    file, and keeps the app alive (the list window is shown whenever the last
    open note is closed, so closing a note never quits the program)."""
    '''
	def init_shortcuts(self):
		#create new note shortcut
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self.create_note())
    '''
        
    def __init__(self):
        self.app = QApplication(sys.argv)
        
        # --- ADD THIS LOGIC TO LOAD THE LOGO ---
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo_sticky.svg")
        if os.path.exists(icon_path):
            self.app.setWindowIcon(QIcon(icon_path))
        # ----------------------------------------
        
        self.notes = []
        self.is_loading = False
        self.list_window = None

        # Debounced disk write: saving the whole JSON on every keystroke and on
        # every pixel of a resize/drag was wasteful.
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._write_notes)

        # Debounced list refresh (also avoids destroying a button while its own
        # click handler is still running).
        self._refresh_timer = QTimer()
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(80)
        self._refresh_timer.timeout.connect(self._do_refresh_list)

        self.app.aboutToQuit.connect(self.flush)
        self.load_notes()

    def show_list_window(self):
        if not self.list_window:
            self.list_window = NotesListWindow(self)
        self.list_window.populate_list()
        self.list_window.show()
        self.list_window.raise_()
        self.list_window.activateWindow()

    def refresh_list_window(self):
        if self.list_window and self.list_window.isVisible():
            self._refresh_timer.start()

    def _do_refresh_list(self):
        if self.list_window and self.list_window.isVisible():
            self.list_window.populate_list()

    def create_note(self, data=None):
        if data is None:
            # cascade new notes so they don't stack exactly on top of each other
            offset = 30 * (len(self.notes) % 8)
            data = {"geometry": [200 + offset, 200 + offset, 280, 320]}
        note = StickyNote(data=data, manager=self)
        self.notes.append(note)

        if note.is_open:
            note.show()
        else:
            note.hide()

        if not self.is_loading:
            self.save_notes()
            self.refresh_list_window()
        return note

    def remove_note(self, note):
        if note not in self.notes:
            return
        self.notes.remove(note)

        # keep the app alive: make sure a window is visible before closing this one
        if not any(n.is_open for n in self.notes):
            self.show_list_window()

        note.close()
        note.deleteLater()
        self.save_notes()
        self.refresh_list_window()

    def save_notes(self):
        if self.is_loading:
            return
        self._save_timer.start()

    def flush(self):
        """Write immediately (used on quit)."""
        self._save_timer.stop()
        self._write_notes()

    def _write_notes(self):
        """Write all notes to disk (temp file + os.replace = atomic)."""
        if self.is_loading:
            return
        data = [n.to_dict() for n in self.notes]
        tmp = NOTES_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
            # atomic write: a crash mid-save can't corrupt the notes file
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, NOTES_FILE)
        except Exception as e:
            print(f"Error saving notes: {e}")

    def load_notes(self):
        """Restore notes from disk; a corrupt file is kept as .bak."""
        self.is_loading = True
        if os.path.exists(NOTES_FILE):
            try:
                with open(NOTES_FILE, "r", encoding="utf-8") as f:
                    content = json.load(f)
                if isinstance(content, dict):
                    notes_data = content.get("notes", [])
                else:
                    notes_data = content

                for note_data in notes_data:
                    if isinstance(note_data, dict):
                        self.create_note(note_data)
            except Exception as e:
                print(f"Error loading notes: {e}")
                # keep the unreadable file instead of overwriting it with an empty list
                try:
                    os.replace(NOTES_FILE, NOTES_FILE + ".bak")
                except OSError:
                    pass

        self.is_loading = False

        if not self.notes:
            self.create_note()

        visible_notes = [n for n in self.notes if n.is_open]
        if not visible_notes:
            self.show_list_window()

    def run(self):
        sys.exit(self.app.exec_())


if __name__ == "__main__":
    manager = StickyNoteManager()
    manager.run()