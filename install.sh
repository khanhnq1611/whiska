#!/bin/bash
set -e

REPO="khanhnguyen/whiska"
BIN="whiska"
INSTALL_DIR="/usr/local/bin"

echo "Installing $BIN..."

URL="https://github.com/$REPO/releases/latest/download/$BIN"
TMP=$(mktemp)

curl -fsSL "$URL" -o "$TMP"
chmod +x "$TMP"

if [ -w "$INSTALL_DIR" ]; then
    mv "$TMP" "$INSTALL_DIR/$BIN"
else
    sudo mv "$TMP" "$INSTALL_DIR/$BIN"
fi

echo "Installed $BIN to $INSTALL_DIR/$BIN"
$BIN --help 2>/dev/null | head -1 || true
