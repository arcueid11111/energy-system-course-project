"""One desktop workspace for local CFD, elastic transients and validation."""

import csv
import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QFormLayout,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QLabel,
    QSlider,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
    QScrollArea,
    QCheckBox,
    QInputDialog,
)
from backend.cfd.schemas import CFDProject, benchmark_project
from backend.transient import TransientSettings
from .physics_view import FieldView, SeriesView

TASKS = [
    ("三维流场 / 压力脉动", "physics_cfd"),
    ("汽蚀—扬程扫描", "physics_scan"),
    ("网格收敛（三层）", "physics_mesh"),
    ("时间步收敛（三层）", "physics_timestep"),
    ("管网弹性瞬变 / 停泵水锤", "physics_transient"),
    ("三维泵—管网双向耦合", "physics_coupled"),
]


def btn(text, callback):
    b = QPushButton(text)
    b.clicked.connect(callback)
    return b


class PhysicsPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.result = None
        self.cfd = None
        self.field_generation = 0
        root = QVBoxLayout(self)
        notice = QLabel(
            "本机独立计算 · OpenFOAM Foundation 14 · 三维有限体积场 / 一维弹性参考分开展示 · 示例不代表实机验证"
        )
        notice.setWordWrap(True)
        root.addWidget(notice)
        split = QSplitter()
        root.addWidget(split, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        left = QWidget()
        scroll.setWidget(left)
        layout = QVBoxLayout(left)
        split.addWidget(scroll)
        form = QFormLayout()
        layout.addLayout(form)
        self.kind = QComboBox()
        self.kind.addItems(["impeller", "cavitating_impeller", "water_hammer", "throttle", "cad"])
        form.addRow("几何 / 物理模式", self.kind)
        self.inputs = {}
        for key, label, value in [
            ("name", "算例名称", "三维旋转基准"),
            ("rpm", "转速 rpm", "600"),
            ("flow_m3h", "整周流量 m³/h", "5"),
            ("inlet_absolute_pa", "入口绝压 Pa", "101325"),
            ("mesh_scale", "网格倍率", "1"),
            ("end_time_s", "时长 s", ".05"),
            ("delta_t_s", "最大步长 s", ".0001"),
            ("write_interval_s", "场输出间隔 s", ".005"),
        ]:
            edit = QLineEdit(value)
            self.inputs[key] = edit
            form.addRow(label, edit)
        self.mode = QComboBox()
        self.mode.addItems(["docker", "native", "wsl"])
        form.addRow("本机求解环境", self.mode)
        self.docker = QLineEdit("docker")
        form.addRow("Docker 命令", self.docker)
        self.context = QLineEdit()
        self.context.setPlaceholderText("可留空；Colima: colima-flowstate-cfd")
        form.addRow("Docker context", self.context)
        self.bashrc = QLineEdit("/opt/openfoam14/etc/bashrc")
        form.addRow("原生 / WSL bashrc", self.bashrc)
        layout.addWidget(btn("检查本机求解器", self.probe))
        self.editor = QPlainTextEdit()
        self.editor.setMinimumHeight(170)
        layout.addWidget(QLabel("完整物理参数 JSON（上方同名字段覆盖）"))
        layout.addWidget(self.editor)
        row = QHBoxLayout()
        row.addWidget(btn("读取参数", self.load_project))
        row.addWidget(btn("保存参数", self.save_project))
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(btn("导入 STEP 流体域", self.import_step))
        row.addWidget(btn("导入分区 STL", self.import_stl))
        layout.addLayout(row)
        layout.addWidget(btn("导出可检查的 OpenFOAM 算例", self.export_case))
        self.task = QComboBox()
        for title, key in TASKS:
            self.task.addItem(title, key)
        layout.addWidget(self.task)
        self.options = QPlainTextEdit()
        self.options.setMinimumHeight(140)
        layout.addWidget(QLabel("任务选项 / 秒级事件 / 耦合条件 JSON"))
        layout.addWidget(self.options)
        layout.addWidget(btn("提交至统一任务队列", self.submit))
        self.history = QComboBox()
        layout.addWidget(self.history)
        row = QHBoxLayout()
        row.addWidget(btn("刷新历史", self.refresh_history))
        row.addWidget(btn("读取历史结果", self.load_history))
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(btn("读取结果 JSON", self.load_result))
        row.addWidget(btn("加载真实场示例", self.load_example))
        layout.addLayout(row)
        layout.addWidget(btn("导出结果 JSON", self.save_result))
        layout.addWidget(btn("导出当前时序 CSV", self.export_series))
        layout.addWidget(btn("导入实测 CSV 进行对比", self.validate_measurement))
        layout.addStretch()
        right = QWidget()
        view = QVBoxLayout(right)
        split.addWidget(right)
        split.setSizes([350, 850])
        controls = QHBoxLayout()
        self.metric = QComboBox()
        self.metric.addItems(["speed_ms", "pressure_pa", "vapour_fraction"])
        self.plane = QComboBox()
        self.plane.addItems(["完整流场", "X 切片", "Y 切片", "Z 切片"])
        self.fraction = QSlider(Qt.Orientation.Horizontal)
        self.fraction.setRange(1, 99)
        self.fraction.setValue(50)
        self.stream = QCheckBox("流线")
        self.vectors = QCheckBox("矢量")
        self.vectors.setChecked(True)
        for w in (self.metric, self.plane, self.fraction, self.stream, self.vectors):
            controls.addWidget(w)
        view.addLayout(controls)
        self.field = FieldView()
        view.addWidget(self.field, 3)
        self.time = QSlider(Qt.Orientation.Horizontal)
        timeline = QHBoxLayout()
        timeline.addWidget(self.time)
        self.play = btn("播放", self.toggle_play)
        timeline.addWidget(self.play)
        view.addLayout(timeline)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(200)
        self.play_timer.timeout.connect(self.advance_frame)
        plotrow = QHBoxLayout()
        self.channel = QComboBox()
        self.channel.addItems(
            [
                "head_m",
                "flow_m3h",
                "pressure_pa",
                "force_x_n",
                "force_y_n",
                "force_z_n",
                "moment_x_nm",
                "moment_y_nm",
                "moment_z_nm",
                "vapour_fraction",
            ]
        )
        self.spectrum = QCheckBox("频谱")
        plotrow.addWidget(self.channel)
        plotrow.addWidget(self.spectrum)
        view.addLayout(plotrow)
        self.plot = SeriesView()
        view.addWidget(self.plot, 2)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(180)
        view.addWidget(self.details)
        self.kind.currentTextChanged.connect(self.preset)
        self.task.currentIndexChanged.connect(self.task_preset)
        for w in (self.metric, self.plane):
            w.currentIndexChanged.connect(self.show_frame)
        self.fraction.sliderReleased.connect(self.show_frame)
        self.stream.toggled.connect(self.show_frame)
        self.time.valueChanged.connect(self.show_frame)
        self.vectors.toggled.connect(
            lambda value: (setattr(self.field, "vectors", value), self.field.update())
        )
        self.channel.currentIndexChanged.connect(self.show_series)
        self.spectrum.toggled.connect(self.show_series)
        self.preset("impeller")
        self.task_preset()

    def guard(self, fn):
        try:
            return fn()
        except Exception as exc:
            self.main.show_error(str(exc))

    def project(self):
        value = json.loads(self.editor.toPlainText())
        value["kind"] = self.kind.currentText()
        for k, w in self.inputs.items():
            value[k] = w.text() if k == "name" else float(w.text())
        value["runtime"].update(
            mode=self.mode.currentText(),
            docker_command=self.docker.text(),
            docker_context=self.context.text(),
            bashrc=self.bashrc.text(),
        )
        return CFDProject.model_validate(value)

    def set_project(self, p):
        self.kind.blockSignals(True)
        self.kind.setCurrentText(p.kind)
        self.kind.blockSignals(False)
        for key, w in self.inputs.items():
            w.setText(str(getattr(p, key)))
        self.mode.setCurrentText(p.runtime.mode)
        self.docker.setText(p.runtime.docker_command)
        self.context.setText(p.runtime.docker_context)
        self.bashrc.setText(p.runtime.bashrc)
        self.editor.setPlainText(p.model_dump_json(indent=2))

    def toggle_play(self):
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play.setText("播放")
        elif self.cfd:
            self.play_timer.start()
            self.play.setText("暂停")

    def advance_frame(self):
        if not self.cfd or self.time.value() >= self.time.maximum():
            self.play_timer.stop()
            self.play.setText("播放")
            return
        if not self.main.workers:
            self.time.setValue(self.time.value() + 1)

    def preset(self, kind):
        if kind == "cad":
            self.details.setPlainText(
                "CAD 模式需先导入已提取的流体体积 STEP，或明确分区的 STL。管网中心线 DXF 仍在原设计页导入。"
            )
            return
        p = benchmark_project(kind)
        p.runtime.mode = self.mode.currentText()
        p.runtime.docker_command = self.docker.text()
        p.runtime.docker_context = self.context.text()
        p.runtime.bashrc = self.bashrc.text()
        self.set_project(p)

    def task_preset(self):
        key = self.task.currentData()
        if key == "physics_scan":
            if self.kind.currentText() not in ("cavitating_impeller", "cad"):
                self.preset("cavitating_impeller")
            if self.kind.currentText() == "cavitating_impeller":
                self.inputs["end_time_s"].setText(
                    str(max(0.6, 6 * 60 / float(self.inputs["rpm"].text())))
                )
                self.inputs["delta_t_s"].setText("0.0001")
                self.inputs["write_interval_s"].setText("0.01")
            value = {"pressures_pa": [101325, 12000, 5000, 2600], "pump_id": "P1"}
        elif key in ("physics_mesh", "physics_timestep"):
            value = {"tolerance": 0.02}
        elif key == "physics_transient":
            value = TransientSettings(
                events=[{"seconds": 0.02, "target": "P1", "kind": "pump_trip"}]
            ).model_dump(mode="json")
        elif key == "physics_coupled":
            value = {
                "pump_id": "P1",
                "max_iterations": 8,
                "relaxation": 0.3,
                "relative_tolerance": 0.02,
                "checkpoint": "",
                "transient": {
                    "duration_s": 0.05,
                    "report_interval_s": 0.001,
                    "start_from_rest": True,
                },
                "start_ramp_s": 0.02,
            }
        else:
            value = {}
        self.options.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))

    def probe(self):
        def start():
            from backend.cfd.runtime import LocalSolver

            p = self.project()
            if p.kind == "water_hammer" and p.wave_solver == "acoustic_fv":
                self.details.setPlainText(
                    "自带三维线性声学有限体积已就绪；此模式不需要安装OpenFOAM或Docker。"
                )
                return
            self.main.work(
                lambda: LocalSolver(p.runtime).probe(self.main.controller.folder / "solver-probe"),
                lambda result: self.details.setPlainText(
                    json.dumps(result, ensure_ascii=False, indent=2)
                ),
            )

        self.guard(start)

    def submit(self):
        def start():
            key = self.task.currentData()
            payload = {
                "options": json.loads(self.options.toPlainText()),
                "document": self.main.document.model_dump(mode="json"),
            }
            if key != "physics_transient":
                payload["project"] = self.project().model_dump(mode="json")
            name = payload.get("project", {}).get("name", self.main.document.name)
            self.main.work(
                lambda: self.main.controller.physics_submit(key, name, payload),
                self.main.task_submitted,
            )

        self.guard(start)

    def refresh_history(self):
        def done(rows):
            self.history.clear()
            for row in rows:
                self.history.addItem(row["created_at"][:19] + " · " + row["name"], row["id"])

        self.main.work(self.main.controller.physics_history, done)

    def load_history(self):
        if self.history.currentData():
            identifier = self.history.currentData()
            self.main.work(
                lambda: self.main.controller.physics_result(identifier),
                self.display_result,
            )

    def display_result(self, result):
        self.result = result
        self.cfd = result.get("cfd", result) if "frames" in result or "cfd" in result else None
        summary = {
            k: v
            for k, v in result.items()
            if k
            not in (
                "frames",
                "loads",
                "pressure_probes",
                "performance",
                "points",
                "cfd",
                "network",
                "measurements",
            )
        }
        if self.cfd:
            summary = {
                k: self.cfd[k]
                for k in ("summary", "verification", "load_analysis")
                if k in self.cfd
            }
            if "load_analysis" in summary:
                summary["load_analysis"] = {
                    k: v for k, v in summary["load_analysis"].items() if k != "axes"
                }
            summary["assumptions"] = self.cfd["manifest"]["assumptions"]
            self.time.setRange(0, len(self.cfd["frames"]) - 1)
            self.time.setValue(len(self.cfd["frames"]) - 1)
            self.show_frame()
        else:
            self.field.set_data(None)
        self.details.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))
        self.show_series()
        self.main.tabs.setCurrentWidget(self)

    def show_frame(self, *args):
        if not self.cfd:
            return
        generation = self.field_generation = self.field_generation + 1
        cfd = self.cfd
        frame = cfd["frames"][self.time.value()]
        metric = self.metric.currentText()
        plane = self.plane.currentIndex() - 1
        fraction = self.fraction.value() / 100
        stream = self.stream.isChecked()

        def read():
            from backend.cfd.fields import display_frame

            p = cfd["manifest"]["project"]
            seeds = p.get("probe_points") or [p.get("fluid_seed", (0, 0, 0))]
            if p["kind"] in ("impeller", "cavitating_impeller"):
                seeds = [(-0.06, 0.004 + i * 0.002, 0.002) for i in range(8)]
            return display_frame(
                cfd["case_directory"],
                frame,
                metric,
                None if plane < 0 else plane,
                fraction,
                seeds,
                streamlines=stream,
            )

        def done(value):
            if generation == self.field_generation:
                self.field.set_data(value)

        self.main.work(
            read,
            done,
            lambda message: (
                self.details.setPlainText(message) if generation == self.field_generation else None
            ),
        )

    def series(self):
        from backend.validation import result_series

        if not self.result:
            return []
        if "map" in self.result:
            return [
                {"seconds": p["npsha_m"], "head_m": p["head_m"], "flow_m3h": p["flow_m3h"]}
                for p in sorted(self.result["map"]["points"], key=lambda r: r["npsha_m"])
            ]
        if (
            "points" in self.result
            and self.result["points"]
            and "nodes" in self.result["points"][0]
        ):
            point = self.result["points"][0]
            node = next(iter(point["nodes"]))
            device = next(iter(point["devices"]), "")
            return result_series(self.result, node_id=node, device_id=device)
        return result_series(self.result)

    def show_series(self, *args):
        def draw():
            field = self.channel.currentText()
            rows = [r for r in self.series() if field in r]
            x = [r["seconds"] for r in rows]
            y = [r[field] for r in rows]
            label = field + " · 秒"
            if self.result and "map" in self.result:
                label = field + " · NPSHa / m"
            if (
                self.spectrum.isChecked()
                and len(rows) >= 4
                and not (self.result and "map" in self.result)
            ):
                from backend.loads import signal_metrics

                metrics = signal_metrics(x, y)
                x = [r["hz"] for r in metrics["spectrum"]]
                y = [r["amplitude"] for r in metrics["spectrum"]]
                label = field + f" · Hz · Δf={metrics['frequency_resolution_hz']:.4g} Hz"
            self.plot.set_series(x, y, label)

        self.guard(draw)

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "读取CFD参数", "", "JSON (*.json)")
        if path:
            self.guard(
                lambda: self.set_project(CFDProject.model_validate_json(Path(path).read_text()))
            )

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存CFD参数", "cfd-project.json", "JSON (*.json)"
        )
        if path:
            self.guard(
                lambda: Path(path).write_text(
                    self.project().model_dump_json(indent=2), encoding="utf-8"
                )
            )

    def export_case(self):
        directory = QFileDialog.getExistingDirectory(self, "选择空目录导出 OpenFOAM 算例")
        if directory:

            def start():
                from backend.cfd.cases import prepare_case

                p = self.project()
                self.main.work(
                    lambda: prepare_case(p, directory),
                    lambda value: self.details.setPlainText(
                        json.dumps(value, ensure_ascii=False, indent=2)
                    ),
                )

            self.guard(start)

    def load_result(self):
        path, _ = QFileDialog.getOpenFileName(self, "读取物理结果", "", "JSON (*.json)")
        if path:

            def load():
                value = json.loads(Path(path).read_text())
                if "frames" in value and not Path(value.get("case_directory", "")).is_dir():
                    value["case_directory"] = str(Path(path).parent)
                self.display_result(value)

            self.guard(load)

    def load_example(self):
        from backend.cfd.cases import resource_root

        path = resource_root() / "examples/cfd/rotor-startup/result.json"

        def read():
            result = json.loads(path.read_text())
            result["case_directory"] = str(path.parent)
            return result

        self.main.work(read, self.display_result)

    def save_result(self):
        if not self.result:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果（场数据仍保存在算例目录）", "physics-result.json", "JSON (*.json)"
        )
        if path:
            self.guard(
                lambda: Path(path).write_text(
                    json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            )

    def export_series(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当前物理时序", "physics-series.csv", "CSV (*.csv)"
        )
        if path:

            def write():
                rows = self.series()
                if not rows:
                    raise ValueError("尚无可导出时序")
                with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, sorted(set.union(*(set(r) for r in rows))))
                    writer.writeheader()
                    writer.writerows(rows)

            self.guard(write)

    def import_stl(self):
        directory = QFileDialog.getExistingDirectory(self, "选择 inlet/outlet/walls 分区STL目录")
        if not directory:
            return
        from backend.cfd.geometry import inspect_stl_directory

        units, ok = QInputDialog.getItem(self, "STL长度单位", "文件实际单位", ["m", "mm"], 0, False)
        if not ok:
            return
        self.main.work(
            lambda: inspect_stl_directory(directory, units),
            lambda info: self.use_geometry(directory, info),
        )

    def use_geometry(self, directory, info):
        bounds = info["bounds_m"]
        seed = [(bounds[i] + bounds[i + 3]) / 2 for i in range(3)]
        value = benchmark_project().model_dump()
        value.update(
            kind="cad",
            provenance="user_geometry",
            source_note="用户导入流体域，测点及网格需校核",
            cad_directory=str(directory),
            cad_bounds=bounds,
            fluid_seed=seed,
            geometry_units=info.get("units", "m"),
        )
        self.set_project(CFDProject.model_validate(value))
        self.details.setPlainText(
            json.dumps(info, ensure_ascii=False, indent=2)
            + "\n请在完整参数中核对fluid_seed、旋转轴与分区；包围盒中心不保证位于流体内部。"
        )

    def import_step(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择已提取的流体体积 STEP", "", "STEP (*.step *.stp)"
        )
        if not path:
            return
        from backend.cfd.geometry import inspect_step

        self.main.work(lambda: inspect_step(path), lambda info: self.step_roles(path, info))

    def step_roles(self, path, info):
        dialog = QDialog(self)
        dialog.setWindowTitle("STEP 面角色（当前单位 m；毫米模型请在转换前选择）")
        outer = QVBoxLayout(dialog)
        units = QComboBox()
        units.addItems(["m", "mm"])
        outer.addWidget(units)
        form = QFormLayout()
        outer.addLayout(form)
        choices = {}
        for face in info["faces"]:
            combo = QComboBox()
            combo.addItems(["walls", "inlet", "outlet", "rotor"])
            choices[face["face"]] = combo
            form.addRow(f"面{face['face']} 中心{[round(x, 4) for x in face['center_m']]}", combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择空目录保存分区STL")
        if not directory:
            return
        from backend.cfd.geometry import export_step_surfaces

        roles = {tag: w.currentText() for tag, w in choices.items()}
        unit = units.currentText()
        self.main.work(
            lambda: export_step_surfaces(path, directory, roles, unit),
            lambda result: self.use_geometry(directory, result),
        )

    def validate_measurement(self):
        if not self.result:
            return self.main.show_error("请先读取需要对比的仿真结果")
        path, _ = QFileDialog.getOpenFileName(self, "选择带明确单位列的实测CSV", "", "CSV (*.csv)")
        if not path:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("实测来源与测点")
        layout = QVBoxLayout(dialog)
        note = QLineEdit()
        note.setPlaceholderText("仪器、测点位置、绝压参考、采样率及数据来源")
        layout.addWidget(note)
        options = QPlainTextEdit(
            '{"probe_index":0,"node_id":"J1","device_id":"P1","tolerances":{}}'
        )
        layout.addWidget(options)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        def submit():
            opts = json.loads(options.toPlainText())
            opts["source_note"] = note.text()
            payload = {
                "csv_text": Path(path).read_text(encoding="utf-8-sig"),
                "result": self.result,
                "options": opts,
            }
            self.main.work(
                lambda: self.main.controller.physics_submit(
                    "physics_validation", "实测对比", payload
                ),
                self.main.task_submitted,
            )

        self.guard(submit)
