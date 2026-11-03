"""Headless check for both source and frozen standalone applications."""

import json
import platform
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent

from backend.cad import import_dxf
from backend.models import default_document
from backend.physics import simulate_design
from desktop.window import MainWindow


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def self_check(app, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "failed",
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    try:
        result = simulate_design(default_document())
        assert result["meta"]["converged"]
        assert result["summary"]["max_mass_residual_m3h"] < 0.001
        cad = import_dxf(
            resource_root() / "examples" / "cad_3d_centerline_demo.dxf", layers=["PIPE_CENTERLINE"]
        )
        assert max(node.z for node in cad.nodes) == 8
        with tempfile.TemporaryDirectory() as temp:
            window = MainWindow(temp)
            window.poll_timer.stop()
            window.resize(1440, 920)
            window.show()
            app.processEvents()
            window.view_mode.setCurrentIndex(1)
            window.display_result(result)
            window.result_selection("link", "P1")
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            window.grab().save(str(output.with_suffix(".png")))
            window.dirty = False
            window.snapshot = {"jobs": []}
            window.close()
            app.processEvents()
        report.update(
            status="passed",
            qt_platform=app.platformName(),
            cad_nodes=len(cad.nodes),
            cad_links=len(cad.links),
            solver=result["meta"]["solver"],
            summary=result["summary"],
            computation="offline",
        )
    except Exception as exc:
        report["error"] = str(exc)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1
