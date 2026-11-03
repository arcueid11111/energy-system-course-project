"""Render the actual Qt widgets with the offscreen platform, without a desktop window."""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from backend.models import default_document
from backend.physics import simulate_design
from desktop.window import MainWindow

app = QApplication([])
reports = ROOT / "reports"
reports.mkdir(exist_ok=True)
assert app.platformName() == "offscreen"
with tempfile.TemporaryDirectory() as temp:
    window = MainWindow(temp)
    window.poll_timer.stop()
    window.resize(1500, 990)
    window.show()
    app.processEvents()
    window.select_element("link", "P1")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    window.grab().save(str(reports / "gui_2d.png"))
    window.view_mode.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(reports / "gui_3d.png"))
    window.display_result(simulate_design(default_document()))
    app.processEvents()
    window.grab().save(str(reports / "gui_result.png"))
    window.dirty = False
    window.snapshot = {"jobs": []}
    window.close()
    app.processEvents()
print("Qt offscreen: 3 actual-widget renders saved; no visible desktop window.")
