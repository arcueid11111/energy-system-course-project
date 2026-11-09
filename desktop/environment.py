"""Repair hidden Qt plugin flags in managed macOS workspaces before Qt discovery."""

import os
import sys
from pathlib import Path


def prepare_qt_plugins():
    if sys.platform != "darwin":
        return
    from PySide6.QtCore import QLibraryInfo

    directory = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    for path in directory.rglob("*.dylib"):
        flags = path.stat().st_flags
        if flags & 0x8000:
            try:
                os.chflags(path, flags & ~0x8000)
            except PermissionError:
                pass
