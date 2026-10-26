#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

VENV=".venv_build"

echo "[*] Setting up build venv..."
python3 -m venv "$VENV"
source "$VENV/bin/activate"

echo "[*] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet pyinstaller
pip install --quiet -r requirements.txt

echo "[*] Building whiska (Linux amd64)..."
pyinstaller whiska.spec --clean --noconfirm

echo "[+] Done: dist/whiska"
ls -lh dist/whiska
