"""Headless check for both source and frozen standalone applications."""

import json
import platform
import sys
import tempfile
import time
import traceback
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
            # Exercise actual VTK reading and the new software field viewer.
            fixture = resource_root() / "examples/cfd/rotor-startup"
            from backend.cfd.fields import display_frame

            physics = json.loads((fixture / "result.json").read_text(encoding="utf-8"))
            physics["case_directory"] = str(fixture)
            field = display_frame(fixture, physics["frames"][-1], plane_axis=0)
            assert field["cell_count"] == 480 and field["maximum"] > 0
            window.physics_panel.display_result(physics)
            window.physics_panel.field.set_data(field)
            app.processEvents()
            window.grab().save(str(output.with_name(output.stem + "-physics.png")))
            # Gmsh must resolve its native library in the frozen distribution too.
            import gmsh

            gmsh.initialize([], readConfigFiles=False, interruptible=False)
            try:
                gmsh.option.setNumber("General.Terminal", 0)
                gmsh.model.occ.addBox(0, 0, 0, 0.2, 0.04, 0.04)
                gmsh.model.occ.synchronize()
                step = Path(temp) / "fluid.step"
                gmsh.write(str(step))
            finally:
                gmsh.finalize()
            from backend.cfd.geometry import inspect_step

            assert len(inspect_step(step)["faces"]) == 6
            from backend.transient import reference_pipe

            wave = reference_pipe()
            assert max(r["valve_pressure_rise_pa"] for r in wave["points"]) > 9e5
            deadline = time.monotonic() + 10
            while window.workers and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
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
            advanced_physics={
                "vtk_cells": 480,
                "gmsh_step_faces": 6,
                "moc_reference": True,
                "solver_runtime_required_for_openfoam": True,
            },
        )
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1
