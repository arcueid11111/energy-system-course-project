"""Record two newly built / newly solved native GUI cases, with a 180 s edit."""

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import pickle
import queue
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
    QVBoxLayout,
    QProgressBar,
)
from PySide6.QtTest import QTest
from backend.models import NetworkDocument
from backend.cfd.schemas import CFDProject
from backend.cfd.fields import display_frame, read_vtk
from desktop.environment import prepare_qt_plugins
from desktop.window import MainWindow
from desktop.focus import ResultFocus
from desktop.canvas import NetworkCanvas
from desktop.physics_view import FieldView, SeriesView
from scripts.record_demo import DataChart, captions, stamp
import imageio_ffmpeg
import numpy as np

OUT = ROOT / "演示视频/双案例实操版"
DATA = OUT / "data"
FPS = 20
TOTAL = 3600
FINAL = OUT / "FLOWSTATE_双案例实机演示_3分钟.mp4"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def narration_scenes(story):
    return [part for scene in story for part in scene.get("voice_parts", [scene])]


def aligned_captions(story):
    return captions(narration_scenes(story))


def field_cache(result):
    from vtkmodules.vtkFiltersCore import vtkCellCenters
    from vtkmodules.util.numpy_support import vtk_to_numpy

    case = result["case_directory"]
    final = result["frames"][-1]
    grid = read_vtk(Path(case) / final["path"])
    centers = vtkCellCenters()
    centers.SetInputData(grid)
    centers.Update()
    points = vtk_to_numpy(centers.GetOutput().GetPoints().GetData())
    seeds = points[np.linspace(0, len(points) - 1, 32, dtype=int)].tolist()
    velocity = [display_frame(case, f, "speed_ms") for f in result["frames"]]
    maxv = max(d["maximum"] for d in velocity)
    for d in velocity:
        d.update(color_minimum=0, color_maximum=maxv)
    pressure = [
        display_frame(
            case,
            final,
            "pressure_pa",
            plane_axis=0,
            plane_fraction=0.15 + i * 0.7 / 15,
            seeds=seeds,
            streamlines=True,
        )
        for i in range(16)
    ]
    lo = min(d["minimum"] for d in pressure)
    hi = max(d["maximum"] for d in pressure)
    for d in pressure:
        d.update(color_minimum=lo, color_maximum=hi)
    return {"velocity": velocity, "pressure": pressure, "source_case": case}


def fit_network(canvas):
    points = np.asarray([canvas.project_world((n.x, n.y, n.z)) for n in canvas.document.nodes])
    if not len(points):
        return
    lo, hi = points[:, :2].min(axis=0), points[:, :2].max(axis=0)
    scale = min(
        max(20, canvas.width() - 220) / max(15, hi[0] - lo[0]),
        max(20, canvas.height() - 150) / max(15, hi[1] - lo[1]),
    )
    canvas._view_scale = scale
    canvas.zoom = 1
    canvas.pan = QPointF(*(-(lo + hi) / 2 * scale))
    canvas.update()


def fit_field(view):
    if view.data is None:
        return
    shell = view.data["surface_points"]
    lo = shell.min(axis=0)
    hi = shell.max(axis=0)
    c, s = math.cos(view.yaw), math.sin(view.yaw)
    a, b = math.cos(view.pitch), math.sin(view.pitch)
    matrix = np.array([[c, -s, 0], [s * b, c * b, -a], [s * a, c * a, b]])
    projected = (shell - (lo + hi) / 2) @ matrix.T
    extent = np.maximum(np.abs(projected[:, :2]).max(axis=0), 1e-12)
    scale = 0.92 * min((view.width() - 140) / 2 / extent[0], (view.height() - 130) / 2 / extent[1])
    view.zoom = (
        scale * max(np.linalg.norm(hi - lo), 1e-12) / min(view.width() - 100, view.height() - 100)
    )
    view.update()


class LiveRecorder:
    def __init__(self, app, rehearse=False, ending_only=False):
        self.app = app
        self.rehearse = rehearse
        self.ending_only = ending_only
        self.frame = 3480 if ending_only else 0
        self.busy = False
        self.errors = []
        self.actions = []
        self.seen = set()
        self.layouts = []
        self.story = json.loads((OUT / "storyboard.json").read_text())
        self.cues = aligned_captions(self.story)
        self.target = NetworkDocument.model_validate(load("network"))
        self.project = CFDProject.model_validate(load("fluid_project"))
        self.take = OUT / "takes" / str(time.time_ns())
        self.take.mkdir(parents=True)
        self.main = MainWindow(self.take)
        self.main.poll_timer.stop()
        self.main.resize(1380, 720)
        self.main.move(30, 55)
        blank = self.target.model_copy(deep=True)
        blank.nodes = []
        blank.links = []
        self.main.set_document(blank, False)
        self.main.show()
        app.processEvents()
        with (DATA / "preflight_fields.pkl").open("rb") as f:
            self.preview = pickle.load(f)
        self.focus = None
        self.cursor = None
        self.clicked = -10
        self.dynamic = None
        self.field = None
        self.current_field = None
        self.slider = None
        self.result = None
        self.high = None
        self.trip_result = None
        self.cfd_result = None
        self.cache = None
        self.cache_loading = False
        self.case_job = None
        self.job_id_by_kind = {}
        self.case_submit_wall = None
        self.events_done = set()
        self.exported = False
        self.wall_start = time.monotonic()
        self.last_poll = 0
        self.last_job_signature = None
        self.progress_target = None
        self.pending_gate_start = None
        self.omitted_waits = []
        self.compact_saved = []
        self.compact_form = None
        if ending_only:
            self.result = json.loads((OUT / "cases/案例一_水力结果.json").read_text())
            self.cfd_result = json.loads((OUT / "cases/案例二_三维求解结果.json").read_text())
            r = self.cfd_result
            self.cache = {
                "velocity": [display_frame(r["case_directory"], r["frames"][-1], "speed_ms")]
            }

        def error(message):
            self.errors.append({"movie_s": self.movie_time(), "error": str(message)})
            print("GUI ERROR", message, flush=True)
            # Keep a failed rehearsal reviewable, and stop a final capture explicitly.

        self.main.show_error = error
        original_submit = self.main.task_submitted

        def submitted(job):
            self.job_id_by_kind[job["kind"]] = job["id"]
            self.actions.append(
                {
                    "movie_s": self.movie_time(),
                    "wall_s": time.monotonic() - self.wall_start,
                    "submitted_job": job,
                }
            )
            original_submit(job)

        self.main.task_submitted = submitted
        self.process = None
        self.writer_error = None
        self.q = queue.Queue(maxsize=20)
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.encoder_log = (OUT / ("rehearsal_encode.log" if rehearse else "encode.log")).open("w")
        if not rehearse:
            self.process = subprocess.Popen(
                [
                    self.ffmpeg,
                    "-y",
                    "-v",
                    "warning",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgba",
                    "-s",
                    "1920x1080",
                    "-r",
                    "20",
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "17",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(OUT / ("结尾补录_无配音.mp4" if ending_only else "实操新录屏_无配音.mp4")),
                ],
                stdin=subprocess.PIPE,
                stderr=self.encoder_log,
            )
            self.writer = threading.Thread(target=self.encode, daemon=True)
            self.writer.start()
        self.schedule = []
        for s in self.story:
            self.event(s["start"], s["action"], getattr(self, s["action"]))
        for i, node in enumerate(self.target.nodes):
            self.event(8.4 + i * 1.65, f"place {node.id}", lambda node=node: self.place_node(node))
        for i, link in enumerate(self.target.links):
            self.event(
                18.5 + i * 2.3, f"connect {link.id}", lambda link=link: self.connect_link(link)
            )
        self.event(33, "rotate 3D", lambda: setattr(self, "dynamic", "network3d"))
        self.event(38.6, "click network solve", self.submit_network)
        self.event(49, "inspect J4", lambda: self.inspect("node", "J4"))
        self.event(54, "inspect J2", lambda: self.inspect("node", "J2"))
        self.event(57, "inspect pump operating point", lambda: self.inspect("link", "P1"))
        self.event(62.5, "batch 1.0 / 1.1", self.submit_batch)
        self.event(65, "show batch comparison", self.show_comparison)
        for when, key, value in [
            (78, "rpm", "600"),
            (80.5, "flow_m3h", "5"),
            (83, "inlet_absolute_pa", "101325"),
            (85, "mesh_scale", "0.5"),
            (87, "end_time_s", "0.3"),
            (89, "delta_t_s", "0.0001"),
            (91, "write_interval_s", "0.01"),
        ]:
            self.event(
                when, f"set CFD {key}", lambda key=key, value=value: self.type_parameter(key, value)
            )
        self.event(93, "probe local OpenFOAM", self.probe_solver)
        self.event(96.6, "click actual CFD solve", self.submit_fluid)
        self.event(147, "submit stop-pump event", self.submit_trip)
        for i, s in enumerate(self.story):
            self.event(s["start"] + 2, f"evidence {i}", lambda i=i: self.evidence(i))
        self.schedule.sort(key=lambda x: x[0])
        if ending_only:
            self.schedule = [
                (3480, "ending", self.ending),
                (3520, "ending evidence", lambda: self.evidence(13)),
            ]
        self.timer = QTimer()
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.capture)
        self.timer.start(12 if rehearse else 50)

    def movie_time(self):
        return self.frame / FPS

    def event(self, t, name, fn):
        self.schedule.append((round(t * FPS), name, fn))

    def encode(self):
        try:
            while True:
                raw = self.q.get()
                if raw is None:
                    break
                self.process.stdin.write(raw)
            self.process.stdin.close()
            self.process.wait()
        except Exception as exc:
            self.writer_error = str(exc)

    def open(self, title, subtitle, cards=(), note=""):
        if self.compact_form is not None:
            for widget, hidden in self.compact_saved:
                widget.setVisible(not hidden)
            panel = self.main.physics_panel
            for widget in [panel.mode, panel.docker, panel.context, panel.bashrc]:
                self.compact_form.setRowVisible(widget, True)
            self.compact_form = None
            self.compact_saved.clear()
        if self.focus:
            self.focus.close()
        self.dynamic = None
        self.field = None
        self.current_field = None
        self.cursor = None
        self.slider = None
        self.progress_target = None
        self.main.play_timer.stop()
        self.main.physics_panel.play_timer.stop()
        self.main.main_scroll.verticalScrollBar().setValue(0)
        self.focus = ResultFocus(self.main, title, subtitle)
        self.focus.set_cards(cards)
        self.focus.note.setText(note)
        self.focus.move(30, 55)
        self.focus.show()
        self.focus.raise_()
        self.app.processEvents()
        return self.focus

    def ready(self):
        self.focus.layout().activate()
        self.focus.body.activate()
        self.app.processEvents()

    def button(self, text, fn):
        b = QPushButton(text)
        b.clicked.connect(fn)
        self.focus.controls.addWidget(b)
        return b

    def mark(self, widget, point=None):
        point = point or widget.rect().center()
        self.cursor = QPointF(widget.mapTo(self.focus, point))
        self.clicked = self.movie_time()

    def click(self, button):
        self.mark(button)
        button.click()

    def camera(self):
        c = self.main.editor
        c._view_center = (30, 0, 0)
        c._view_scale = 8
        c.zoom = 1
        c.pan = QPointF()
        c.update()

    def export(self, name, data):
        folder = OUT / "cases"
        folder.mkdir(exist_ok=True)
        save(folder / f"{name}.json", data)

    def poll(self):
        now = time.monotonic()
        if now - self.last_poll < 0.35:
            return
        self.last_poll = now
        self.snapshot = self.main.controller.snapshot()
        self.main.refreshed((self.snapshot, self.main.controller.runs()))
        signature = [
            (j["id"], j["state"], round(j["progress"], 3), j["message"])
            for j in self.snapshot["jobs"]
        ]
        if signature != self.last_job_signature:
            self.actions.append(
                {
                    "movie_s": self.movie_time(),
                    "wall_s": now - self.wall_start,
                    "actual_job_states": signature,
                }
            )
            self.last_job_signature = signature
        for job in self.snapshot["jobs"]:
            if job["state"] == "failed":
                raise RuntimeError(job["name"] + ": " + str(job["error"]))
        if self.progress_target:
            job = self.find(self.progress_target)
            if job:
                state = {"queued": "等待执行", "running": "正在计算", "succeeded": "计算完成"}.get(
                    job["state"], job["state"]
                )
                self.focus.subtitle.setText(f"{state} · {job['message']}")
                if hasattr(self, "progress_bar"):
                    self.progress_bar.setValue(round(job["progress"] * 100))

    def find(self, kind, name=None):
        jobs = self.main.controller.snapshot()["jobs"]
        if name is not None:
            return next((j for j in jobs if j["kind"] == kind and j["name"] == name), None)
        ident = self.job_id_by_kind.get(kind)
        return next((j for j in jobs if j["id"] == ident), None)

    def get_network(self):
        if self.result is not None:
            return True
        j = self.find("design_simulation")
        if not j or j["state"] != "succeeded":
            return False
        self.result = self.main.controller.result(j["result"]["design_run_id"])
        self.export("案例一_水力结果", self.result)
        return True

    def get_high(self):
        if self.high is not None:
            return True
        j = self.find("design_simulation", self.target.name + " · 1.10 倍速")
        if not j or j["state"] != "succeeded":
            return False
        self.high = self.main.controller.result(j["result"]["design_run_id"])
        self.export("案例一_1.1倍速结果", self.high)
        return True

    def get_fluid(self):
        if self.cache is not None:
            return True
        if self.rehearse:
            self.cfd_result = load("fluid_preflight")
            self.cache = self.preview
            return True
        j = self.find("physics_cfd")
        if not j or j["state"] != "succeeded":
            return False
        if not self.cache_loading:
            self.cache_loading = True
            self.cfd_result = self.main.controller.physics_result(j["result"]["physics_result_id"])
            self.export("案例二_三维求解结果", self.cfd_result)

            def ready(cache):
                self.cache = cache

            self.main.work(lambda: field_cache(self.cfd_result), ready)
        return False

    def get_trip(self):
        if self.trip_result is not None:
            return True
        j = self.find("physics_transient")
        if not j or j["state"] != "succeeded":
            return False
        self.trip_result = self.main.controller.physics_result(j["result"]["physics_result_id"])
        self.export("案例一_停泵结果", self.trip_result)
        return True

    def gate(self):
        if self.ending_only:
            return True
        t = self.movie_time()
        checks = [
            (45, self.get_network),
            (65, self.get_high),
            (112, self.get_fluid),
            (158, self.get_trip),
        ]
        for start, check in checks:
            if t >= start and not check():
                return False
        return True

    def intro(self):
        f = self.open(
            "今天，从两个工程案例开始",
            "三维建网 → 本机计算 → 结果解读",
            [
                ("案例一", "多高程供水管网", "5节点 / 1台泵 / 3段管道"),
                ("案例二", "三维叶轮流场", "600rpm / 5m³·h⁻¹ / 0.3s"),
            ],
            "FLOWSTATE / 一套工作台连接设计、求解、可视化与运行分析",
        )
        net = NetworkCanvas(self.target, editable=False)
        net.set_dimension("3d")
        f.add_view(net, 1)
        self.field = FieldView()
        d = dict(self.preview["velocity"][0])
        d["metric"] = "网格预览"
        self.field.set_data(d)
        self.field.vectors = False
        self.field.zoom = 1.35
        f.add_view(self.field, 1)
        self.ready()
        net.fit()
        fit_network(net)
        fit_field(self.field)

    def nodes(self):
        f = self.open(
            "案例一 / 从空白工程构建三维供水管网",
            "先放节点，再填写坐标、高程和需水量",
            [
                ("水源", "2 m", "水源总水头"),
                ("三个用水节点", "3 / 6 / 9 m", "分别设置水力高程"),
                ("总需水量", "9 m³/h", "3 + 2 + 4 m³/h"),
            ],
            "正在实际编辑当前工程；坐标、高程和参数将直接提交给水力求解器",
        )
        f.add_view(self.main.editor, 3, True)
        f.add_view(self.main.inspector_scroll, 1, True)
        self.mode = QComboBox()
        self.mode.addItems(["选择 / 移动", "放置水源", "放置用水节点", "连接离心泵", "连接管道"])
        self.mode.currentIndexChanged.connect(
            lambda i: self.main.tool.setCurrentIndex(
                self.main.tool.findData(["select", "reservoir", "junction", "pump", "pipe"][i])
            )
        )
        f.controls.addWidget(self.mode)
        self.apply = self.button("应用参数", self.main.apply_properties)
        self.button(
            "二维 / 三维",
            lambda: self.main.editor.set_dimension(
                "3d" if self.main.editor.dimension == "2d" else "2d"
            ),
        )
        self.ready()
        self.main.editor.set_dimension("2d")
        self.camera()

    def place_node(self, node):
        self.mode.setCurrentIndex(1 if node.kind == "reservoir" else 2)
        self.camera()
        c = self.main.editor
        point = c.screen((node.x, node.y, 0)).toPoint()
        self.mark(c, point)
        QTest.mouseClick(c, Qt.MouseButton.LeftButton, pos=point)
        assert self.main.selected == ("node", node.id)
        for key in ["x", "y", "z", "elevation_m", "demand_m3h", "source_head_m"]:
            if key in self.main.fields:
                self.main.fields[key].setValue(getattr(node, key))
        self.click(self.apply)
        self.main.inspector_scroll.verticalScrollBar().setValue(0)
        self.camera()
        self.actions.append(
            {"movie_s": self.movie_time(), "created_node": node.model_dump(mode="json")}
        )

    def links(self):
        self.focus.title.setText("把泵、支路与高程连接成一个完整模型")
        self.focus.subtitle.setText("离心泵P1 + 3段DN50管道 · 高程与管长自动进入同一工程")

    def connect_link(self, link):
        self.mode.setCurrentIndex(3 if link.kind == "pump" else 4)
        self.camera()
        c = self.main.editor
        for key in [link.start, link.end]:
            n = next(n for n in self.main.document.nodes if n.id == key)
            p = c.screen((n.x, n.y, n.z)).toPoint()
            self.mark(c, p)
            QTest.mouseClick(c, Qt.MouseButton.LeftButton, pos=p)
        assert self.main.selected == ("link", link.id)
        self.main.inspector_scroll.verticalScrollBar().setValue(0)
        self.actions.append(
            {"movie_s": self.movie_time(), "created_link": link.model_dump(mode="json")}
        )

    def geometry(self):
        self.main.editor.set_dimension("3d")
        self.main.editor.fit()
        fit_network(self.main.editor)
        self.main.select_element("link", "P1")
        self.mode.setCurrentIndex(0)
        self.focus.title.setText("三维空间关系与运行参数一起检查")
        self.focus.subtitle.setText(
            "水力高程3/6/9m · 管径50mm · 泵额定转速2900rpm · 末端压力要求15m"
        )
        assert digest(self.main.document.model_dump(mode="json")) == digest(
            self.target.model_dump(mode="json")
        )
        self.export("案例一_三维管网工程", self.main.document.model_dump(mode="json"))
        self.actions.append(
            {
                "movie_s": self.movie_time(),
                "network_matches_target": True,
                "document_sha256": digest(self.main.document.model_dump(mode="json")),
            }
        )

    def queue_view(self, title, kind, note):
        f = self.open(title, "本机任务队列", [], note)
        f.add_view(self.main.jobs_table, 3, True)
        table = self.main.jobs_table
        table.setColumnHidden(4, True)
        table.setColumnWidth(0, 300)
        table.setColumnWidth(1, 120)
        table.setColumnWidth(2, 90)
        table.setColumnWidth(3, 400)
        side = QWidget()
        layout = QVBoxLayout(side)
        label = QLabel("当前任务进度")
        layout.addWidget(label)
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        layout.addStretch()
        f.add_view(side, 1)
        self.progress_target = kind
        self.ready()
        self.last_poll = 0
        self.poll()

    def network_solve(self):
        self.queue_view(
            "将刚才搭建的工程提交水力求解",
            "design_simulation",
            "工程快照、求解任务和结果一起保存在本机",
        )
        self.solve_button = self.button("开始本机水力计算", self.main.submit_simulation)

    def submit_network(self):
        self.click(self.solve_button)

    def network_results(self):
        assert self.get_network()
        self.main.display_result(self.result)
        s = self.result["summary"]
        p = self.result["points"][0]
        f = self.open(
            "案例一结果 / 先看供水和最高处的压力",
            "点击节点查看压力，点击泵查看Q—H运行点",
            [
                ("实际供水", f"{p['delivered_m3h']:.2f} m³/h", "目标9m³/h"),
                ("最高节点J4压力", f"{p['nodes']['J4']['pressure_m']:.2f} m", "要求≥15m"),
                ("1小时用电", f"{s['energy_kwh']:.3f} kWh", "当前方案"),
                ("供水结果", "需水已满足", f"未满足量 {s['unmet_m3']:.3f} m³"),
            ],
            "案例一 / 本次新建工程的实际EPANET计算结果",
        )
        f.add_view(self.main.result_canvas, 3, True)
        f.add_view(self.main.trend, 2, True)
        self.main.result_canvas.set_dimension("3d")
        self.main.result_canvas.fit()
        fit_network(self.main.result_canvas)
        self.main.trend.key = "pump_curve"
        self.main.trend.update()
        self.ready()
        self.dynamic = "results3d"

    def inspect(self, kind, key):
        c = self.main.result_canvas
        if kind == "node":
            n = next(n for n in c.document.nodes if n.id == key)
            p = c.screen((n.x, n.y, n.z)).toPoint()
        else:
            link = next(l for l in c.document.links if l.id == key)
            pts = c.line_points(link)
            p = ((pts[0] + pts[-1]) / 2).toPoint()
        self.mark(c, p)
        QTest.mouseClick(c, Qt.MouseButton.LeftButton, pos=p)
        assert c.selection == (kind, key), f"节点或设备点击未命中：{key}"
        point = self.result["points"][0]
        if kind == "node":
            n = next(n for n in c.document.nodes if n.id == key)
            v = point["nodes"][key]
            self.focus.note.setText(
                f"{key}：高程{n.elevation_m:.0f}m / 压力{v['pressure_m']:.2f}m / 实际供水{v['demand_m3h']:.2f}m³/h"
            )
        else:
            v = point["links"][key]
            self.focus.note.setText(
                f"P1运行点：Q={v['flow_m3h']:.2f}m³/h / H={v['head_m']:.2f}m / 电功率{v['power_kw']:.3f}kW"
            )
        self.actions.append(
            {
                "movie_s": self.movie_time(),
                "inspect": key,
                "values": point["nodes" if kind == "node" else "links"][key],
            }
        )

    def comparison(self):
        self.focus.title.setText("同一个管网，继续比较运行方案")
        self.focus.subtitle.setText("批量提交1.0和1.1倍速，比较供水、末端压力与电能")
        self.batch_button = self.button("批量计算1.0 / 1.1倍速", self.main.submit_batch)
        self.main.batch_values.setText("1.0,1.1")

    def submit_batch(self):
        self.click(self.batch_button)

    def show_comparison(self):
        assert self.get_high()
        low = self.result
        high = self.high
        saving = 100 * (1 - low["summary"]["energy_kwh"] / high["summary"]["energy_kwh"])
        f = self.open(
            "两种转速：供水相同，电能相差22.1%",
            "案例一 / 同边界、同一小时计算时长",
            [
                ("额定转速电能", f"{low['summary']['energy_kwh']:.3f} kWh", "1.0倍速"),
                ("1.1倍速电能", f"{high['summary']['energy_kwh']:.3f} kWh", "1.1倍速"),
                ("本例用电减少", f"{saving:.1f}%", "比较两次实际计算"),
                ("额定转速末端压力", f"{low['points'][0]['min_pressure_m']:.2f} m", "≥15m"),
            ],
            "两次任务均来自本轮操作生成的工程；点击计算后从队列读取结果",
        )
        f.add_view(
            DataChart(
                "泵组电功率",
                [
                    ("1.0倍速", [(p["hour"], p["power_kw"]) for p in low["points"]], "#60ddc0"),
                    ("1.1倍速", [(p["hour"], p["power_kw"]) for p in high["points"]], "#edb877"),
                ],
                "时间 / h",
                "kW",
            ),
            1,
        )
        f.add_view(
            DataChart(
                "最低末端压力",
                [
                    (
                        "1.0倍速",
                        [(p["hour"], p["min_pressure_m"]) for p in low["points"]],
                        "#60ddc0",
                    ),
                    (
                        "1.1倍速",
                        [(p["hour"], p["min_pressure_m"]) for p in high["points"]],
                        "#edb877",
                    ),
                    ("压力要求", [(0, 15), (1, 15)], "#bba7f0"),
                ],
                "时间 / h",
                "m",
            ),
            1,
        )
        self.ready()

    def fluid_setup(self):
        p = self.project.model_copy(deep=True)
        p.rpm = 300
        p.flow_m3h = 4
        p.mesh_scale = 1
        p.end_time_s = 0.05
        p.write_interval_s = 0.005
        self.main.physics_panel.set_project(p)
        self.main.physics_panel.task.setCurrentIndex(0)
        self.main.physics_panel.options.setPlainText("{}")
        f = self.open(
            "案例二 / 实际填写叶轮工况并启动求解",
            "水 / 单相层流 / 30°叶轮扇区 / OpenFOAM 14",
            [],
            "填写工况后，点击开始三维计算。",
        )
        f.add_view(self.main.physics_panel.parameter_scroll, 1, True)
        self.main.physics_panel.parameter_scroll.setMinimumWidth(440)
        self.main.physics_panel.parameter_scroll.verticalScrollBar().setValue(0)
        panel = self.main.physics_panel
        layout = panel.parameter_scroll.widget().layout()
        self.compact_form = layout.itemAt(0).layout()
        for widget in [panel.mode, panel.docker, panel.context, panel.bashrc]:
            self.compact_form.setRowVisible(widget, False)

        def hide_item(item):
            if item.widget() is not None:
                widget = item.widget()
                self.compact_saved.append((widget, widget.isHidden()))
                widget.hide()
            elif item.layout() is not None:
                child = item.layout()
                for i in range(child.count()):
                    hide_item(child.itemAt(i))

        for i in range(1, layout.count()):
            hide_item(layout.itemAt(i))
        panel.inputs["name"].setCursorPosition(0)
        self.field = FieldView()
        d = dict(self.preview["velocity"][0])
        d["metric"] = "网格几何预览"
        self.field.set_data(d)
        self.field.vectors = False
        self.field.zoom = 1.42
        f.add_view(self.field, 2)
        self.probe_button = self.button("检查本机求解器", self.main.physics_panel.probe)
        self.fluid_button = self.button("开始三维计算", self.main.physics_panel.submit)
        self.ready()
        fit_field(self.field)

    def type_parameter(self, key, value):
        w = self.main.physics_panel.inputs[key]
        self.mark(w)
        w.setFocus()
        w.selectAll()
        QTest.keyClicks(w, value)
        self.main.physics_panel.parameter_scroll.verticalScrollBar().setValue(0)
        self.actions.append(
            {"movie_s": self.movie_time(), "typed_parameter": key, "value": w.text()}
        )

    def probe_solver(self):
        self.click(self.probe_button)

    def fluid_solve(self):
        p = self.main.physics_panel.project()
        assert p.model_dump(mode="json") == self.project.model_dump(mode="json")
        self.export("案例二_三维工况设置", p.model_dump(mode="json"))
        self.actions.append(
            {
                "movie_s": self.movie_time(),
                "fluid_parameters_verified": True,
                "project_sha256": digest(p.model_dump(mode="json")),
            }
        )
        self.queue_view(
            "三维计算 / 网格生成、检查、求解、导出",
            "physics_cfd",
            "计算等待已加速；任务状态与进度来自正在运行的本机求解器",
        )
        self.fluid_start = self.button("启动本机OpenFOAM计算", self.main.physics_panel.submit)

    def submit_fluid(self):
        self.case_submit_wall = time.monotonic()
        if self.rehearse:
            self.focus.subtitle.setText("界面排练 / 三维结果视图检查")
            return
        self.click(self.fluid_start)

    def fluid_results(self):
        assert self.get_fluid()
        r = self.cfd_result
        p = r["manifest"]["project"]
        assert p == self.project.model_dump(mode="json")
        f = self.open(
            "案例二结果 / 刚才这次任务的三维流场",
            "速度场回放 → 压力切片与流线",
            [
                ("工况", f"{p['rpm']:.0f} rpm", f"整周流量{p['flow_m3h']:.0f}m³/h"),
                ("网格", f"{r['summary']['cells']:,} 单元", "网格检查通过"),
                ("完整时间轴", "0—0.3 s", f"{len(r['frames'])}个实际输出时刻"),
                ("入口绝压", "101325 Pa", "水 / 单相层流"),
            ],
            "同一任务：设置、求解、网格、时序与场结果一一对应",
        )
        self.field = FieldView()
        self.field.zoom = 1.5
        f.add_view(self.field, 3)
        self.flow_plot = SeriesView()
        f.add_view(self.flow_plot, 1)
        self.metric = QComboBox()
        self.metric.addItems(["速度场 + 矢量", "压力切片 + 流线"])
        self.metric.currentIndexChanged.connect(
            lambda i: self.draw_field("velocity" if i == 0 else "pressure", 0)
        )
        f.controls.addWidget(self.metric)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 30)
        self.slider.valueChanged.connect(self.slide_field)
        f.controls.addWidget(self.slider, 1)
        self.dynamic = "fluid"
        self.ready()

    def slide_field(self, index):
        kind = "velocity" if self.metric.currentIndex() == 0 else "pressure"
        self.draw_field(kind, min(index, len(self.cache[kind]) - 1))

    def draw_field(self, kind, index):
        if self.current_field == (kind, index):
            return
        self.current_field = (kind, index)
        d = self.cache[kind][index]
        self.field.set_data(d)
        self.metric.blockSignals(True)
        self.metric.setCurrentIndex(0 if kind == "velocity" else 1)
        self.metric.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(self.cache[kind]) - 1)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        if kind == "velocity":
            rows = self.cache[kind][: index + 1]
            self.flow_plot.set_series(
                [r["seconds"] for r in rows], [r["maximum"] for r in rows], "最大速度 / m·s⁻¹"
            )
            self.focus.subtitle.setText(f"本次求解的速度场 · t={d['seconds']:.3f}s")
        else:
            rows = [r for r in self.cfd_result["performance"] if r["seconds"] >= 0.15]
            self.flow_plot.set_series(
                [r["seconds"] for r in rows], [r["head_m"] for r in rows], "进出口总水头差 / m"
            )
            self.focus.subtitle.setText(f"本次求解的压力切片 + 流线 · t={d['seconds']:.3f}s")
        self.actions.append(
            {
                "movie_s": self.movie_time(),
                "field": kind,
                "index": index,
                "physical_time_s": d["seconds"],
                "source_case": self.cache["source_case"],
            }
        )

    def loads(self):
        r = self.cfd_result
        axes = r["load_analysis"]["axes"]
        m = axes["force_x"]
        rows = [row for row in r["loads"] if row["seconds"] >= 0.09]
        spectrum = [(x["hz"], x["amplitude"]) for x in m["spectrum"] if x["hz"] <= 150]
        f = self.open(
            "同一次三维计算，继续查看载荷与频谱",
            "30°叶轮扇区 / X向流体力",
            [
                ("平均流体力", f"{m['mean']:.3f} N", "壁面压力与黏性积分"),
                ("脉动均方根", f"{m['rms_fluctuation']:.4f} N", "当前统计窗口"),
                ("峰峰值", f"{m['peak_to_peak']:.3f} N", "载荷时序分析"),
            ],
            "查看载荷变化和频谱后，可继续切换其他方向的力与力矩",
        )
        f.add_view(
            DataChart(
                "X向流体力",
                [("Fx", [(x["seconds"], x["force_n"][0]) for x in rows], "#64ddc1")],
                "时间 / s",
                "N",
            ),
            1,
        )
        f.add_view(DataChart("对应频谱", [("幅值", spectrum, "#edb871")], "频率 / Hz", "N"), 1)
        self.trip_button = self.button("对案例一增加停泵分析", self.start_trip)
        self.ready()

    def submit_trip(self):
        self.click(self.trip_button)

    def start_trip(self):
        p = self.main.physics_panel
        p.task.setCurrentIndex(4)
        p.options.setPlainText(
            json.dumps(
                {
                    "duration_s": 0.1,
                    "max_dt_s": 0.0001,
                    "report_interval_s": 0.001,
                    "events": [{"seconds": 0.01, "target": "P1", "kind": "pump_trip"}],
                }
            )
        )
        p.submit()

    def trip(self):
        assert self.get_trip()
        r = self.trip_result
        rows = r["points"]
        minimum = min(x["nodes"]["J4"]["pressure_pa"] for x in rows) / 1e6
        f = self.open(
            "继续案例一 / 这套管网停泵后会发生什么？",
            "原供水工程 + 0.010s停泵事件",
            [
                ("事件设备", "P1", "第一个案例的同一台泵"),
                ("停泵时刻", "0.010 s", "惯性降速"),
                ("J4最低绝压", f"{minimum:.3f} MPa", "0—0.1s计算窗口"),
                ("输出时刻", str(len(rows)) + " 个", "压力与转速同步查看"),
            ],
            "当前曲线来自本轮提交的管网弹性瞬变计算任务",
        )
        f.add_view(
            DataChart(
                "最高用水点J4压力",
                [
                    (
                        "绝对压力",
                        [(x["seconds"], x["nodes"]["J4"]["pressure_pa"] / 1e6) for x in rows],
                        "#64ddc2",
                    )
                ],
                "时间 / s",
                "MPa",
            ),
            1,
        )
        f.add_view(
            DataChart(
                "P1转速响应",
                [
                    (
                        "转速比",
                        [(x["seconds"], x["devices"]["P1"]["speed_ratio"]) for x in rows],
                        "#eeb771",
                    )
                ],
                "时间 / s",
                "n/n₀",
            ),
            1,
        )
        self.ready()

    def ending(self):
        self.export("案例一_三维管网工程", self.result["document"])
        self.export("案例二_三维工况设置", self.project.model_dump(mode="json"))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["hour", "delivered_m3h", "power_kw", "min_pressure_m"])
        for p in self.result["points"]:
            w.writerow([p["hour"], p["delivered_m3h"], p["power_kw"], p["min_pressure_m"]])
        (OUT / "cases/案例一_水力时序.csv").write_text(buf.getvalue())
        self.exported = True
        f = self.open(
            "两个案例，从设计一路算到结果",
            "工程、任务、参数与结果已保存在本机",
            [
                ("案例一", "三维建网 + 水力求解", "节点压力 / 运行点 / 转速比较"),
                ("案例二", "三维设定 + 本机求解", "速度场 / 压力切片 / 载荷"),
                ("继续分析", "停泵事件", "同一工程串联运行与瞬态"),
            ],
            "FLOWSTATE / 三维设计 · 本机求解 · 联动可视化 · 工程与结果导出",
        )
        self.main.display_result(self.result)
        f.add_view(self.main.result_canvas, 1, True)
        self.main.result_canvas.set_dimension("3d")
        self.main.result_canvas.fit()
        fit_network(self.main.result_canvas)
        v = FieldView()
        v.set_data(self.cache["velocity"][-1])
        v.zoom = 1.35
        f.add_view(v, 1)
        self.ready()
        fit_field(v)
        fit_network(self.main.result_canvas)
        self.dynamic = "ending3d"

    def motion(self, t):
        if self.dynamic in ["network3d", "results3d", "ending3d"]:
            c = self.main.editor if self.dynamic == "network3d" else self.main.result_canvas
            c.yaw = 0.5 + (t - 30) * 0.018
            c.pitch = 0.4
            fit_network(c)
        if self.dynamic == "fluid":
            if t < 134:
                index = min(30, max(0, int((t - 112) / 21 * 30)))
                kind = "velocity"
            else:
                index = min(15, max(0, int((t - 134) / 11 * 15)))
                kind = "pressure"
            self.draw_field(kind, index)
            self.field.yaw = -0.7 + (t - 112) * 0.032
            self.field.pitch = 0.45 + 0.1 * math.sin((t - 112) * 0.15)
            self.field.update()
            fit_field(self.field)

    def evidence(self, index):
        self.check_layout(index)
        self.render(self.movie_time()).save(str(OUT / "frames" / f"{index:02}.png"))

    def check_layout(self, index):
        rect = self.focus.rect()
        views = []
        for i in range(self.focus.body.count()):
            w = self.focus.body.itemAt(i).widget()
            if w is None:
                continue
            g = w.geometry()
            good = rect.contains(g) and g.width() >= 250 and g.height() >= 180 and w.isVisible()
            views.append(
                {
                    "class": type(w).__name__,
                    "rect": g.getRect(),
                    "visible": w.isVisible(),
                    "within_window": good,
                }
            )
            if not good:
                raise RuntimeError("主视图未完整显示: " + str(views[-1]))
            if isinstance(w, NetworkCanvas):
                for node in w.document.nodes:
                    point = w.screen((node.x, node.y, node.z))
                    if not (
                        10 <= point.x() <= w.width() - 10 and 10 <= point.y() <= w.height() - 35
                    ):
                        raise RuntimeError("管网节点超出主图范围：" + node.id)
        self.layouts.append(
            {
                "chapter": index,
                "views": views,
                "outer_scroll": self.main.main_scroll.verticalScrollBar().value(),
            }
        )

    def render(self, t):
        image = QImage(1920, 1080, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#0c1823"))
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = next((s for s in self.story if s["start"] <= t < s["end"]), self.story[-1])
        p.fillRect(0, 0, 1920, 5, QColor("#59d6b6"))
        p.setFont(QFont("PingFang SC", 23, QFont.Weight.DemiBold))
        p.setPen(QColor("#eff8f4"))
        p.drawText(42, 45, "FLOWSTATE / " + s["title"])
        p.setFont(QFont("PingFang SC", 13))
        p.setPen(QColor("#a4c7b9"))
        p.drawText(
            QRectF(1470, 14, 405, 40),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"实操案例  {int(t) // 60:02}:{int(t) % 60:02} / 03:00",
        )
        raw = self.focus.grab().toImage()
        area = QRectF(40, 77, 1840, 900)
        scale = min(area.width() / self.focus.width(), area.height() / self.focus.height())
        ww, hh = self.focus.width() * scale, self.focus.height() * scale
        dest = QRectF(area.center().x() - ww / 2, area.center().y() - hh / 2, ww, hh)
        p.drawImage(dest, raw)
        if self.cursor is not None and 0 <= t - self.clicked < 0.8:
            pos = QPointF(dest.x() + self.cursor.x() * scale, dest.y() + self.cursor.y() * scale)
            p.setPen(QPen(QColor("#e3ac42"), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(pos, 20, 20)
        p.fillRect(0, 991, 1920, 89, QColor("#0c1823"))
        p.setFont(QFont("PingFang SC", 23, QFont.Weight.Medium))
        p.setPen(QColor("#f0f8f5"))
        text = next((txt for a, b, txt in self.cues if a <= t < b), "")
        p.drawText(
            QRectF(60, 994, 1800, 74), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text
        )
        p.fillRect(0, 1076, round(1920 * t / 180), 4, QColor("#59d6b6"))
        p.end()
        return image

    def capture(self):
        if self.busy:
            return
        self.busy = True
        try:
            if self.frame >= TOTAL:
                self.finish()
                return
            self.poll()
            if not self.gate():
                if self.pending_gate_start is None:
                    self.pending_gate_start = time.monotonic()
                if time.monotonic() - self.pending_gate_start > 300:
                    raise RuntimeError("等待计算结果超时")
                return
            if self.pending_gate_start is not None:
                self.omitted_waits.append(
                    {
                        "movie_s": self.movie_time(),
                        "wall_s": time.monotonic() - self.pending_gate_start,
                    }
                )
                self.pending_gate_start = None
            for index, (frame, name, fn) in enumerate(self.schedule):
                if self.frame >= frame and index not in self.events_done:
                    self.events_done.add(index)
                    self.actions.append({"movie_s": self.movie_time(), "action": name})
                    fn()
            t = self.movie_time()
            self.motion(t)
            if self.ending_only and self.frame % 20 == 0:
                self.check_layout("ending")
            # Real solver progress sampled over a longer wall interval; the edit is 16s.
            interval = 12 if self.rehearse else (200 if 96 <= t < 112 else 50)
            if self.timer.interval() != interval:
                self.timer.setInterval(interval)
            if not self.rehearse:
                image = self.render(t)
                self.q.put(bytes(image.constBits()))
                if self.current_field:
                    self.seen.add(self.current_field)
            self.frame += 1
        except Exception as exc:
            self.errors.append(
                {
                    "movie_s": self.movie_time(),
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            print(traceback.format_exc(), flush=True)
            self.finish()
        finally:
            self.busy = False

    def finish(self):
        self.timer.stop()
        if self.process:
            self.q.put(None)
            self.writer.join(timeout=90)
            if self.writer_error:
                self.errors.append({"error": self.writer_error})
            if self.process.poll() is None:
                self.process.kill()
                self.errors.append({"error": "encoder timeout"})
        jobs = self.main.controller.snapshot()["jobs"]
        case_job = self.find("physics_cfd")
        report = {
            "rehearsal": self.rehearse,
            "qt_platform": self.app.platformName(),
            "native_visible_window": True,
            "frames": self.frame,
            "frames_captured": self.frame - (3480 if self.ending_only else 0),
            "ending_patch": self.ending_only,
            "duration_s": self.frame / FPS,
            "wall_seconds": time.monotonic() - self.wall_start,
            "errors": self.errors,
            "workspace": str(self.take),
            "input_source": "network constructed through native GUI; fresh job results",
            "cfd_case": self.cfd_result["case_directory"] if self.cfd_result else None,
            "actual_cfd_job": case_job,
            "captured_fields": {
                k: sorted(i for kind, i in self.seen if kind == k) for k in ["velocity", "pressure"]
            },
            "omitted_waits": self.omitted_waits,
            "layouts": self.layouts,
            "actions": self.actions,
            "jobs": jobs,
            "exports_saved": self.exported,
        }
        filename = (
            "ending_recording.json"
            if self.ending_only
            else ("rehearsal.json" if self.rehearse else "recording.json")
        )
        save(OUT / filename, report)
        if self.focus:
            self.focus.close()
        self.main.dirty = False
        self.main.snapshot = {"jobs": []}
        self.main.close()
        self.app.processEvents()
        print("LIVE CASE CAPTURE FINISHED", self.frame, "errors", len(self.errors), flush=True)
        self.app.quit()


def compose():
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    story = json.loads((OUT / "storyboard.json").read_text())
    voice = narration_scenes(story)
    r = json.loads((OUT / "recording.json").read_text())
    assert r["frames"] == 3600 and not r["errors"]
    assert r["actual_cfd_job"]["state"] == "succeeded"
    assert (
        len(r["captured_fields"]["velocity"]) == 31 and len(r["captured_fields"]["pressure"]) == 16
    )
    source = OUT / "实操新录屏_无配音.mp4"
    if (OUT / "ending_recording.json").exists():
        patch = json.loads((OUT / "ending_recording.json").read_text())
        assert not patch["errors"] and patch["frames_captured"] == 120
        assert patch["cfd_case"] == r["cfd_case"]
        joined = OUT / "实操合并录屏_无配音.mp4"
        subprocess.run(
            [
                ff,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-i",
                str(OUT / "结尾补录_无配音.mp4"),
                "-filter_complex",
                "[0:v]trim=end=174,setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]",
                "-map",
                "[v]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "17",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "20",
                "-movflags",
                "+faststart",
                str(joined),
            ],
            check=True,
        )
        source = joined
        save(
            OUT / "edit_log.json",
            {
                "segments": [
                    {"source": "实操新录屏_无配音.mp4", "movie_s": [0, 174]},
                    {
                        "source": "结尾补录_无配音.mp4",
                        "movie_s": [174, 180],
                        "same_result_case": r["cfd_case"],
                    },
                ],
                "reason": "结尾固定居中，所有管网节点保持可见；使用同一次任务结果重新录制原生视窗",
            },
        )
    cues = aligned_captions(story)
    ass = OUT / "对齐字幕.ass"
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Heiti SC,28,&H00F5F8F0,&H00F5F8F0,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,2,70,70,22,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

    def ass_time(t):
        cs = round(t * 100)
        return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"

    rows = []
    for a, b, text in cues:
        if len(text) > 48:
            cut = len(text) // 2
            text = text[:cut] + r"\N" + text[cut:]
        rows.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{text}")
    ass.write_text(header + "\n".join(rows) + "\n")
    args = [ff, "-y", "-v", "warning", "-i", str(source)]
    for s in voice:
        args += ["-i", s["audio"]]
    filters = [
        f"[{i + 1}:a]adelay={round((s['start'] + 0.3) * 1000)}:all=1[a{i}]"
        for i, s in enumerate(voice)
    ]
    filters.append(
        "".join(f"[a{i}]" for i in range(len(voice)))
        + f"amix=inputs={len(voice)}:duration=longest:normalize=0,apad,atrim=duration=180,loudnorm=I=-16:TP=-1.5:LRA=9[a]"
    )
    filters.append(
        f"[0:v]drawbox=x=0:y=991:w=1920:h=85:color=0x0c1823:t=fill,subtitles=filename='{ass}':fontsdir='/System/Library/Fonts'[v]"
    )
    args += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "17",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "160k",
        "-t",
        "180",
        "-movflags",
        "+faststart",
        str(FINAL),
    ]
    subprocess.run(args, check=True)
    (OUT / "中文字幕.srt").write_text(
        "\n\n".join(
            f"{i + 1}\n{stamp(a)} --> {stamp(b)}\n{text}" for i, (a, b, text) in enumerate(cues)
        )
        + "\n"
    )
    (OUT / "配音与章节.md").write_text(
        "# 双案例实机演示\n\n"
        + "\n\n".join(
            f"## {s['start'] // 60:02}:{s['start'] % 60:02}—{s['end'] // 60:02}:{s['end'] % 60:02} {s['title']}\n\n{s['text']}"
            for s in story
        )
    )
    print("COMPOSED", FINAL, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rehearse", action="store_true")
    p.add_argument("--compose", action="store_true")
    p.add_argument("--prepare-fields", action="store_true")
    p.add_argument("--ending-only", action="store_true")
    args = p.parse_args()
    (OUT / "frames").mkdir(parents=True, exist_ok=True)
    if args.prepare_fields:
        data = field_cache(load("fluid_preflight"))
        with (DATA / "preflight_fields.pkl").open("wb") as f:
            pickle.dump(data, f)
        print("Prepared geometry and rehearsal fields")
    elif args.compose:
        compose()
    else:
        prepare_qt_plugins()
        app = QApplication([sys.argv[0]])
        app.setFont(QFont("PingFang SC", 12))
        recorder = LiveRecorder(app, args.rehearse, args.ending_only)
        app.exec()
        if recorder.errors:
            raise SystemExit(1)
