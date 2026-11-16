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
    # A cold VTK import may exceed five seconds on a freshly started process.
    wait_qt(lambda: panel.field.data is not None, seconds=15)
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


@pytest.mark.parametrize("width,height", [(1280, 720), (1024, 600), (800, 500)])
def test_small_window_can_scroll_to_bottom_on_every_tab(window, width, height):
    from PySide6.QtCore import QPoint

    window.resize(width, height)
    QApplication.processEvents()
    assert window.height() <= height
    assert window.width() <= width
    area = window.main_scroll
    for i in range(window.tabs.count()):
        window.tabs.setCurrentIndex(i)
        QApplication.processEvents()
        bar = area.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(bar.maximum())
        QApplication.processEvents()
        top = window.log.mapTo(area.viewport(), QPoint()).y()
        assert top >= 0
        assert top + window.log.height() <= area.viewport().height()


def test_main_view_responds_to_mouse_wheel(window):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    window.resize(1024, 600)
    QApplication.processEvents()
    area = window.main_scroll
    bar = area.verticalScrollBar()
    bar.setValue(0)
    point = area.viewport().rect().center()
    event = QWheelEvent(
        QPointF(point),
        QPointF(area.viewport().mapToGlobal(point)),
        QPoint(),
        QPoint(0, -240),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(area.viewport(), event)
    assert bar.value() > 0


def test_long_parameter_panels_keep_bottom_buttons_reachable(window):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton

    window.resize(1024, 600)
    window.select_element("link", "P1")
    QApplication.processEvents()
    inspector = window.inspector_scroll
    button = next(b for b in window.inspector.findChildren(QPushButton) if b.text() == "应用参数")
    inspector.ensureWidgetVisible(button)
    QApplication.processEvents()
    assert 0 <= button.mapTo(inspector.viewport(), QPoint()).y()
    assert (
        button.mapTo(inspector.viewport(), QPoint()).y() + button.height()
        <= inspector.viewport().height()
    )

    panel = window.physics_panel
    window.tabs.setCurrentWidget(panel)
    QApplication.processEvents()
    area = panel.parameter_scroll
    assert area.verticalScrollBar().maximum() > 0
    last = next(b for b in area.findChildren(QPushButton) if b.text() == "导入实测 CSV 进行对比")
    area.ensureWidgetVisible(last)
    window.main_scroll.ensureWidgetVisible(last)
    QApplication.processEvents()
    assert 0 <= last.mapTo(area.viewport(), QPoint()).y()
    assert last.mapTo(area.viewport(), QPoint()).y() + last.height() <= area.viewport().height()
    assert (
        last.mapTo(window.main_scroll.viewport(), QPoint()).y() + last.height()
        <= window.main_scroll.viewport().height()
    )


@pytest.mark.parametrize("kind", ["settings", "step_faces"])
def test_long_dialogs_scroll_without_hiding_confirm_buttons(window, monkeypatch, kind):
    from PySide6.QtWidgets import QDialog, QScrollArea, QDialogButtonBox
    from PySide6.QtCore import QPoint

    inspected = []

    def inspect(dialog):
        dialog.resize(680, 400)
        dialog.show()
        QApplication.processEvents()
        area = dialog.findChild(QScrollArea)
        assert area is not None
        assert area.verticalScrollBar().maximum() > 0
        box = dialog.findChild(QDialogButtonBox)
        assert not area.isAncestorOf(box)
        assert box.mapTo(dialog, QPoint()).y() + box.height() <= dialog.height()
        area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
        assert area.verticalScrollBar().value() > 0
        inspected.append(True)
        dialog.hide()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect)
    if kind == "settings":
        window.edit_settings()
    else:
        window.physics_panel.step_roles(
            "unused.step",
            {"faces": [{"face": i + 1, "center_m": [0.0, 0.0, float(i)]} for i in range(100)]},
        )
    assert inspected


@pytest.mark.parametrize("canvas_name,tab", [("editor", 0), ("physics", 5)])
def test_wheel_over_canvas_scrolls_page_and_modifier_zooms(window, canvas_name, tab):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    window.resize(1024, 600)
    window.tabs.setCurrentIndex(tab)
    QApplication.processEvents()
    canvas = window.editor if canvas_name == "editor" else window.physics_panel.field
    bar = window.main_scroll.verticalScrollBar()
    bar.setValue(0)
    initial = canvas.zoom

    def wheel(modifiers):
        pos = canvas.rect().center()
        event = QWheelEvent(
            QPointF(pos),
            QPointF(canvas.mapToGlobal(pos)),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            modifiers,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(canvas, event)

    wheel(Qt.KeyboardModifier.NoModifier)
    assert bar.value() > 0
    assert canvas.zoom == initial
    for modifier in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.MetaModifier):
        bar.setValue(0)
        before = canvas.zoom
        wheel(modifier)
        assert canvas.zoom < before
        assert bar.value() == 0


def test_result_visualization_modes_profile_and_diagnostics(window):
    from backend.models import default_document
    from backend.physics import simulate_design

    w = window
    doc = default_document()
    for link in doc.links:
        if link.kind == "pipe":
            link.diameter_m = 0.025
    w.display_result(simulate_design(doc))
    assert w.result_canvas.color_metric == "combined"
    assert {"pressure", "flow", "velocity", "headloss"} <= set(w.result_canvas.ranges)
    for i in range(w.color_mode.count()):
        w.color_mode.setCurrentIndex(i)
        w.result_canvas.repaint()
    assert w.diagnostics["findings"] and w.result_canvas.flags
    assert w.kpis["health"].value.text() != "正常"
    w.result_canvas.set_animate(True)
    w.result_canvas.phase = 1.3
    w.result_canvas.repaint()
    w.trend_metric.setCurrentIndex(w.trend_metric.findData("profile"))
    w.result_selection("node", "J5")
    profile = w.trend.profile
    ids = [s["id"] for s in profile["stations"]]
    assert ids[0] == "R1" and ids[-1] == "J5" and "J2" in ids
    j1, j2 = (next(s for s in profile["stations"] if s["id"] == k) for k in ("J1", "J2"))
    assert j2["head"] > j1["head"] and j2["pump"]
    w.trend.repaint()
    w.trend_metric.setCurrentIndex(w.trend_metric.findData("element"))
    assert w.trend.series and "J5" in w.trend.label
    first = w.diagnostics["findings"][0]
    w.pick_finding(first["target_kind"], first["target"], first["first_hour"])
    assert w.result_element == (first["target_kind"], first["target"])
    assert w.element_table.rowCount() > 3
    w.result_canvas.set_animate(False)


def test_tool_palette_browser_and_inspector_units(window):
    w = window
    w.tool.setCurrentIndex(w.tool.findData("pipe"))
    assert w.tool_buttons.checkedId() == w.tool.currentIndex()
    assert w.editor.mode == "pipe"
    w.tool_buttons.button(0).click()
    assert w.editor.mode == "select"
    w.browser.picked.emit("link", "L2")
    assert w.selected == ("link", "L2")
    assert w.fields["diameter_m"].value() == 40  # shown in mm
    w.fields["diameter_m"].setValue(50)
    w.apply_properties()
    assert next(l for l in w.document.links if l.id == "L2").diameter_m == 0.05
    w.select_element("link", "P1")
    rows = w.fields["curve"].value()
    assert rows[0]["flow_m3h"] == 0 and len(rows) == 5
    w.validate_document()
    assert w.design_flags == {}


def test_optimization_dialog_ranks_feasible_lowest_energy_first(window):
    from backend.jobs import optimize_design
    from backend.models import default_document

    doc = default_document()
    value = optimize_design(doc)
    dialog = window.show_optimization(value)
    from PySide6.QtWidgets import QTableWidget

    table = dialog.findChild(QTableWidget)
    assert table.rowCount() == len(value["candidates"])
    assert table.item(0, 4).text().startswith("✓")
    feasible = [c["energy_kwh"] for c in value["candidates"] if c["feasible"]]
    assert float(table.item(0, 2).text()) == pytest.approx(min(feasible), abs=0.01)
    dialog.close()


def test_failed_diagnostics_do_not_show_normal_health(window, monkeypatch):
    import backend.diagnostics
    from backend.models import default_document
    from backend.physics import simulate_design

    def unavailable(result):
        raise ValueError("diagnostic unavailable")

    monkeypatch.setattr(backend.diagnostics, "diagnose", unavailable)
    window.display_result(simulate_design(default_document()))
    assert window.kpis["health"].value.text() == "诊断未完成"
    assert window.result_canvas.point is not None
    assert "diagnostic unavailable" in window.kpis["health"].toolTip()


def test_hydraulic_profile_excludes_closed_pipe_and_valve(window):
    from backend.models import DesignLink, default_document
    from backend.physics import simulate_design

    result = simulate_design(default_document())
    # Closed shortcuts must not supersede the open supply route in the profile.
    for kind in ("pipe", "valve"):
        link = DesignLink(id=f"closed_{kind}", kind=kind, start="R1", end="J5",
                          length_m=1, enabled=False)
        result["document"]["links"].append(link.model_dump(mode="json"))
        for point in result["points"]:
            point["links"][link.id] = {"active": False, "flow_m3h": 0}
    window.display_result(result)
    window.result_selection("node", "J5")
    assert [s["id"] for s in window.hydraulic_profile()["stations"]] == [
        "R1", "J1", "J2", "J5"
    ]
