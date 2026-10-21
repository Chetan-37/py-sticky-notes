#!/bin/bash

INSTALL_DIR="$HOME/.local/share/py-sticky-notes"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "=== Installing PySticky Notes ==="

# 1. Check for Python3
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is required but not installed."
    exit 1
fi

# 2. Create directories
mkdir -p "$INSTALL_DIR/assets"
mkdir -p "$DESKTOP_DIR"

# 3. Copy application files and assets
if [ -f "sticky.py" ]; then
    cp sticky.py "$INSTALL_DIR/"
else
    echo "Error: sticky.py not found in current folder!"
    exit 1
fi

if [ -f "assets/logo.png" ]; then
    cp "assets/logo.png" "$INSTALL_DIR/assets/"
fi

# 4. CRITICAL FIX: Give execution permission to the script so it never throws 'Permission denied'
chmod +x "$INSTALL_DIR/sticky.py"

# 5. Install PyQt5 locally in user space (no sudo required)
echo "Installing/verifying PyQt5 dependency..."
python3 -m pip install --user --upgrade PyQt5

# 6. Create Xubuntu Application Menu Entry
DESKTOP_FILE="$DESKTOP_DIR/pystickynotes.desktop"
cat << EOF > "$DESKTOP_FILE"
[Desktop Entry]
Name=Sticky Notes
Comment=Lightweight Windows-style sticky notes built with Python
Exec=python3 $INSTALL_DIR/sticky.py
Icon=$INSTALL_DIR/assets/logo.png
Terminal=false
Type=Application
Categories=Utility;TextEditor;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

# 7. Add a shortcut to the Desktop if it exists
if [ -d "$HOME/Desktop" ]; then
    cp "$DESKTOP_FILE" "$HOME/Desktop/"
    chmod +x "$HOME/Desktop/pystickynotes.desktop"
    echo "Added shortcut to Desktop."
fi

echo "=== Installation Complete! ==="
echo "You can now find 'Sticky Notes' in your Xubuntu Application Menu."