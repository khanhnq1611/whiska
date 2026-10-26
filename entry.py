"""PyInstaller entry point — absolute imports for single-file build."""
import sys
import os

# Ensure the package directory is on sys.path
sys.path.insert(0, os.path.join(sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(__file__)))

from whiskacli.__main__ import main

if __name__ == "__main__":
    main()
