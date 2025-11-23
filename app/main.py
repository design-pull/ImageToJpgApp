# app/main.py
# Entry point for the ImageToJpgApp GUI
# Requires: PyQt5

import sys
import os
from pathlib import Path
from PyQt5 import QtWidgets, QtCore
from app.gui.main_window import MainWindow

APP_NAME = "ImageToJpgApp"

def resource_path(rel_path: str) -> str:
    """Resolve path relative to project root (simple helper)."""
    base = Path(__file__).resolve().parents[2]
    return str(base.joinpath(rel_path))

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    # Optional: load QSS theme
    qss_path = resource_path("resources/styles/theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())

    window = MainWindow()
    window.resize(800, 500)
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
