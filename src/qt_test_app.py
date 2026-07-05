"""Shared Qt bootstrap for tests (not a test module itself).

All Qt-touching test modules must create their app through ensure_qapp():
only one Q*Application can exist per process, and a plain QCoreApplication
created by an earlier module in the discovery order would block widget
construction in later ones. Offscreen platform so no display is required.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


def ensure_qapp() -> QApplication:
    return QApplication.instance() or QApplication([])
