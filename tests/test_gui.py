"""Qt offscreen interaction checks. No visible desktop window or browser."""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop.window import MainWindow


@pytest.fixture(scope="module")
def app():
    from desktop.environment import prepare_qt_plugins

    prepare_qt_plugins()
    instance = QApplication.instance() or QApplication([])
    assert instance.platformName() == "offscreen"
    yield instance


@pytest.fixture
def window(app, tmp_path):
    w = MainWindow(tmp_path / "desktop")
    w.poll_timer.stop()
    w.resize(1440, 920)
    w.show()
    app.processEvents()
    yield w
    w.dirty = False
    w.snapshot = {"jobs": []}
    w.close()
    app.processEvents()


def wait_qt(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return
        QTest.qWait(20)
    raise AssertionError("Qt 状态等待超时")


def test_editor_canvas_add_connect_properties_and_undo(window):
    w = window
    before = len(w.document.nodes)
    w.tool.setCurrentIndex(w.tool.findData("junction"))
    QTest.mouseClick(w.editor, Qt.MouseButton.LeftButton, pos=w.editor.rect().center())
    assert len(w.document.nodes) == before + 1
    identifier = w.selected[1]
    w.add_link("pipe", "J2", identifier)
    assert w.document.links[-1].end == identifier
    w.select_element("node", identifier)
    w.fields["z"].setValue(7)
    w.fields["elevation_m"].setValue(6)
    w.apply_properties()
    n = next(n for n in w.document.nodes if n.id == identifier)
    assert (n.z, n.elevation_m) == (7, 6)
    w.undo()
    n = next(n for n in w.document.nodes if n.id == identifier)
    assert n.z == 0
    w.redo()
    assert next(n for n in w.document.nodes if n.id == identifier).z == 7


def test_native_queue_result_and_3d_projection(window):
    w = window
    w.submit_simulation()
    wait_qt(lambda: any(j["state"] == "succeeded" for j in w.controller.snapshot()["jobs"]))
    state = w.controller.snapshot()
    job = state["jobs"][0]
    run = w.controller.result(job["result"]["design_run_id"])
    w.display_result(run)
    assert w.current_result["document"]["nodes"][-1]["z"] == 8
    w.timeline.setValue(4)
    assert w.result_canvas.point["seconds"] == 1200
    canvas = w.result_canvas
    assert canvas.project_world((1, 2, 0)) != canvas.project_world((1, 2, 5))
    assert run["summary"]["unmet_m3"] < 0.005
    assert w.tabs.currentIndex() == 1
    w.result_selection("link", "P1")
    assert w.trend.key == "pump_curve"
    assert w.trend.pump_id == "P1"
    w.trend.repaint()
    QApplication.processEvents()


def test_native_project_save_load_preserves_three_dimensions(window, tmp_path):
    w = window
    path = tmp_path / "network.json"
    w.file_path = path
    w.save_project()
    assert path.exists()
    w.document.nodes[-1].z = 100
    w.load_project(path)
    assert w.document.nodes[-1].z == 8
    assert w.dirty is False


def test_split_pipe_preserves_length_and_hydraulic_solution(window):
    from backend.physics import simulate_design

    w = window
    before = simulate_design(w.document)
    original = next(l for l in w.document.links if l.id == "L2")
    length = original.length_m
    w.select_element("link", "L2")
    w.split_selected_pipe()
    assert len(w.document.nodes) == 7 and len(w.document.links) == 7
    node = w.selected[1]
    pieces = [l for l in w.document.links if node in (l.start, l.end)]
    assert len(pieces) == 2 and sum(l.length_m for l in pieces) == pytest.approx(length)
    after = simulate_design(w.document)
    assert after["summary"]["energy_kwh"] == pytest.approx(
        before["summary"]["energy_kwh"], rel=1e-4
    )


def test_desktop_work_directory_excludes_duplicate_instance(tmp_path):
    from desktop.controller import Controller

    first = Controller(tmp_path / "shared")
    try:
        with pytest.raises(ValueError, match="另一个 GUI"):
            Controller(tmp_path / "shared")
    finally:
        first.close()
    second = Controller(tmp_path / "shared")
    second.close()


def test_physics_workspace_actual_fields_and_task_options(window):
    import json

    panel = window.physics_panel
    panel.load_example()
    wait_qt(lambda: panel.field.data is not None)
    assert panel.field.data["cell_count"] == 480
    assert panel.plot.rows
    panel.channel.setCurrentText("force_x_n")
    panel.spectrum.setChecked(True)
    assert panel.plot.rows and "Hz" in panel.plot.label
    panel.task.setCurrentIndex(1)
    assert panel.kind.currentText() == "cavitating_impeller"
    assert panel.project().end_time_s >= 0.6
    assert len(json.loads(panel.options.toPlainText())["pressures_pa"]) >= 4
    panel.preset("water_hammer")
    assert panel.project().wave_solver == "acoustic_fv"
    assert panel.project().end_time_s < 0.031
    panel.plane.setCurrentIndex(1)
    wait_qt(lambda: not window.workers)


def test_transient_view_selects_node_pipe_and_device(window):
    from backend.transient import simulate_transient, TransientSettings

    r = simulate_transient(
        window.document, TransientSettings(duration_s=0.004, report_interval_s=0.001)
    )
    panel = window.physics_panel
    panel.display_result(r)
    assert panel.location.currentData()[0] == "node"
    assert panel.location.currentData()[1] != "R"
    panel.channel.setCurrentText("pressure_pa")
    assert panel.plot.rows
    for i in range(panel.location.count()):
        item = panel.location.itemData(i)
        if item[0] == "pipe":
            panel.location.setCurrentIndex(i)
            assert (
                panel.series()[0]["pressure_pa"]
                == r["points"][0]["pipes"][item[1]]["pressure_pa"][item[2]]
            )
            break
    panel.display_result({"accepted_for_reduced_model": False, "rejection_reasons": ["unsettled"]})
    assert "校核未通过" in panel.assessment.text()
