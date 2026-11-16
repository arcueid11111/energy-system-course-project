"""Fresh 180-second customer demo, recording real native FLOWSTATE widgets.

--rehearse exercises the same UI at 4x speed without encoding.
--compose adds newly synthesized narration; no old video/audio is reused.
"""

import argparse
import json
import math
from pathlib import Path
import pickle
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QSlider, QWidget
from PySide6.QtTest import QTest
from backend.models import NetworkDocument
from desktop.environment import prepare_qt_plugins
from desktop.window import MainWindow
from desktop.focus import ResultFocus
from desktop.physics_view import FieldView, SeriesView
import imageio_ffmpeg
import numpy as np

OUT = ROOT / "演示视频"
ASSETS = OUT / "assets"
DURATION = 180
FPS = 20
WIDTH, HEIGHT = 1920, 1080
FINAL = OUT / "FLOWSTATE_客户演示_中文配音_3分钟.mp4"


def read(name):
    return json.loads((ASSETS / f"{name}.json").read_text())


def stamp(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def captions(story):
    import re

    result = []
    for s in story:
        parts = [p.strip() for p in re.findall(r"[^。！？]+[。！？]?", s["text"]) if p.strip()]
        total = sum(map(len, parts))
        start = s["start"] + 0.3
        for part in parts:
            end = start + s["audio_duration"] * len(part) / total
            result.append((start, end, part))
            start = end
    return result


class DataChart(QWidget):
    """Plot supplied result arrays with visible legends and units."""

    def __init__(self, title, series, xlabel="", ylabel="", bounds=None, cursor=None):
        super().__init__()
        self.title, self.series = title, series
        self.xlabel, self.ylabel = xlabel, ylabel
        self.bounds, self.cursor = bounds, cursor
        self.setMinimumSize(380, 240)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#101c2b"))
        p.setFont(QFont("PingFang SC", 14))
        p.setPen(QColor("#ecf4f3"))
        p.drawText(20, 29, self.title)
        x = 22
        for label, rows, color in self.series:
            p.setPen(QPen(QColor(color), 3))
            p.drawLine(x, 53, x + 20, 53)
            p.setPen(QColor("#c2d6df"))
            p.setFont(QFont("PingFang SC", 11))
            p.drawText(x + 26, 58, label)
            x += 32 + len(label) * 12
        valid = [np.asarray(rows, dtype=float) for _, rows, _ in self.series if len(rows)]
        if not valid:
            p.end()
            return
        a = np.concatenate(valid)
        xmin, ymin = a.min(axis=0)
        xmax, ymax = a.max(axis=0)
        if self.bounds:
            xmin, xmax, ymin, ymax = self.bounds
        else:
            pad = max((ymax - ymin) * 0.12, abs(ymax) * 0.035, 1e-7)
            ymin -= pad
            ymax += pad
        dx, dy = max(xmax - xmin, 1e-12), max(ymax - ymin, 1e-12)
        r = QRectF(80, 90, self.width() - 110, self.height() - 158)
        p.setFont(QFont("PingFang SC", 11))
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor("#304458"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#bdd0dc"))
            p.drawText(5, int(y + 4), f"{ymin + dy * i / 4:.3g}")

        def point(x, y):
            return QPointF(
                r.left() + (x - xmin) / dx * r.width(), r.bottom() - (y - ymin) / dy * r.height()
            )

        for label, rows, color in self.series:
            rows = np.asarray(rows, dtype=float)
            if len(rows) < 2:
                continue
            # Preserve impact extrema when the signal has more samples than pixels.
            stride = max(1, len(rows) // max(2, int(r.width())))
            points = []
            for j in range(0, len(rows), stride):
                b = rows[j : j + stride]
                ids = sorted({0, len(b) - 1, int(b[:, 1].argmin()), int(b[:, 1].argmax())})
                points.extend(b[ids])
            p.setPen(QPen(QColor(color), 2.2))
            p.drawPolyline(QPolygonF([point(x, y) for x, y in points]))
        if self.cursor is not None:
            q = point(self.cursor, ymin)
            p.setPen(QPen(QColor("#f2d684"), 1, Qt.PenStyle.DashLine))
            p.drawLine(q, QPointF(q.x(), r.top()))
        p.setPen(QColor("#bdd0dc"))
        for i in range(5):
            x = r.left() + r.width() * i / 4
            p.drawText(
                QRectF(x - 32, r.bottom() + 8, 75, 25),
                Qt.AlignmentFlag.AlignCenter,
                f"{xmin + dx * i / 4:.3g}",
            )
        p.drawText(
            QRectF(r.left(), self.height() - 29, r.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            self.xlabel,
        )
        p.drawText(15, 79, self.ylabel)
        p.end()


class CandidateChart(QWidget):
    def __init__(self, candidates):
        super().__init__()
        self.rows = []
        self.setMinimumSize(540, 320)
        for count in (1, 2):
            for speed in (0.8, 0.9, 1.0, 1.1):
                self.rows.append(
                    next(
                        c
                        for c in candidates
                        if sum(c["mode"]) == count and c["speed_ratio"] == speed
                    )
                )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#101c2b"))
        p.setFont(QFont("PingFang SC", 14))
        p.setPen(QColor("#e5f2f0"))
        p.drawText(20, 29, "组合与转速扫描 · 先满足供水和压力，再比较能耗")
        p.setFont(QFont("PingFang SC", 11))
        p.setPen(QColor("#63dcc1"))
        p.drawText(22, 57, "绿色：可行")
        p.setPen(QColor("#eeaa65"))
        p.drawText(145, 57, "橙色：供水 / 压力不达标")
        r = QRectF(70, 85, self.width() - 95, self.height() - 160)
        ymax = max(c["energy_kwh"] for c in self.rows) * 1.15
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor("#304458"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#bdd0dc"))
            p.drawText(6, int(y + 4), f"{ymax * i / 4:.2f}")
        bw = r.width() / len(self.rows)
        for i, c in enumerate(self.rows):
            h = r.height() * c["energy_kwh"] / ymax
            x = r.left() + i * bw
            color = "#63dcc1" if c["feasible"] else "#eeaa65"
            p.fillRect(QRectF(x + bw * 0.18, r.bottom() - h, bw * 0.64, h), QColor(color))
            p.setPen(QColor("#dae8ea"))
            p.drawText(
                QRectF(x - 4, r.bottom() - h - 23, bw + 8, 20),
                Qt.AlignmentFlag.AlignCenter,
                f"{c['energy_kwh']:.2f}",
            )
            p.drawText(
                QRectF(x - 4, r.bottom() + 8, bw + 8, 45),
                Qt.AlignmentFlag.AlignCenter,
                f"{'单泵' if sum(c['mode']) == 1 else '双泵'}\n{c['speed_ratio']:.1f}倍速",
            )
        p.setPen(QColor("#bdd0dc"))
        p.drawText(8, 78, "kWh")
        p.end()


class Recorder:
    def __init__(self, app, rehearse=False, flow_only=False):
        self.app = app
        self.rehearse = rehearse
        self.flow_only = flow_only
        self.offset = 70 if flow_only else 0
        self.duration = 35 if flow_only else DURATION
        self.speed = 4 if rehearse else 1
        self.story = json.loads((OUT / "storyboard.json").read_text())
        self.cues = captions(self.story)
        self.source = {
            name: read(name)
            for name in [
                "baseline",
                "reference_high_speed",
                "operating_replay",
                "optimization",
                "calibration",
                "calibrated_run",
                "observations",
                "rotor",
                "cavitating",
                "cavitation",
                "transient",
                "wave",
                "coupled",
                "customer_claims",
            ]
        }
        self.base_doc = NetworkDocument.model_validate(self.source["baseline"]["document"])
        self.take = OUT / "takes" / str(time.time_ns())
        self.take.mkdir(parents=True)
        ids = list(read("manifest")["jobs"].values())
        for name in ["desktop.sqlite3", "desktop-dispatch.sqlite3"]:
            shutil.copy2(OUT / "workspace" / name, self.take / name)
        with sqlite3.connect(self.take / "desktop-dispatch.sqlite3") as con:
            con.execute(
                "DELETE FROM jobs WHERE id NOT IN (" + ",".join("?" for _ in ids) + ")", ids
            )
            con.execute("UPDATE scheduler SET paused=0")
        self.main = MainWindow(self.take)
        self.main.poll_timer.stop()
        self.main.resize(1380, 720)
        self.main.move(30, 55)
        self.main.set_document(self.base_doc.model_copy(deep=True), False)
        self.main.show()
        self.app.processEvents()
        self.focus = None
        self.errors = []
        self.actions = []
        self.seen = set()
        self.current_replay = None
        with (ASSETS / "replay.pkl").open("rb") as f:
            self.replay = pickle.load(f)
        with (ASSETS / "wave_replay.pkl").open("rb") as f:
            self.wave_frames = pickle.load(f)
        self.started = False
        self.frames = 0
        self.process = None
        self.writer_error = None
        self.work_queue = queue.Queue(maxsize=24)
        self.cursor = None
        self.click_at = -10
        self.bad_layouts = []
        self.chart = None
        self.field = None
        self.dynamic = None
        self.log = (OUT / ("rehearsal_encode.log" if rehearse else "encode.log")).open("w")
        original = self.main.show_error

        def show_error(message):
            self.errors.append({"time": self.elapsed(), "error": str(message)})
            original(message)

        self.main.show_error = show_error
        if not rehearse:
            self.process = subprocess.Popen(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
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
                    str(FPS),
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
                    str(OUT / ("三维补录_无配音.mp4" if flow_only else "新录屏_无配音.mp4")),
                ],
                stdin=subprocess.PIPE,
                stderr=self.log,
            )
            self.writer = threading.Thread(target=self.encode, daemon=True)
            self.writer.start()
        self.timer = QTimer()
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.capture)
        self.start_clock = time.perf_counter()
        self.started = True
        if flow_only:
            self.later(0, self.flow)
            self.later(2, lambda: self.evidence(4))
        else:
            for i, s in enumerate(self.story):
                self.at(s["start"], getattr(self, s["action"]), s["action"])
                self.at(s["start"] + 2, lambda i=i: self.evidence(i), "layout evidence")
            self.at(179.4, self.check_jobs, "queue verification")
            self.at(180, self.finish, "finish")
        self.timer.start(50 if not rehearse else 160)

    def elapsed(self):
        return (
            (time.perf_counter() - self.start_clock) * self.speed + self.offset
            if self.started
            else 0
        )

    def later(self, seconds, fn):
        timer = QTimer(self.main)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(lambda: self.safe(fn))
        timer.timeout.connect(timer.deleteLater)
        timer.start(max(1, round(seconds * 1000 / self.speed)))

    def at(self, seconds, fn, name):
        def action():
            self.actions.append({"time": round(self.elapsed(), 3), "action": name})
            fn()

        self.later(seconds, action)

    def safe(self, fn):
        try:
            fn()
        except Exception as exc:
            err = {"time": self.elapsed(), "error": str(exc), "traceback": traceback.format_exc()}
            self.errors.append(err)
            print(err["traceback"], flush=True)

    def encode(self):
        try:
            while True:
                data = self.work_queue.get()
                if data is None:
                    break
                self.process.stdin.write(data)
            self.process.stdin.close()
            self.process.wait()
        except Exception as exc:
            self.writer_error = str(exc)

    def open_focus(self, title, subtitle, cards, note):
        if self.focus:
            self.focus.close()
        self.chart = None
        self.field = None
        self.dynamic = None
        self.current_replay = None
        self.cursor = None
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
        for canvas in [self.main.editor, self.main.result_canvas]:
            canvas.fit()
        self.app.processEvents()

    def button(self, text, fn):
        b = QPushButton(text)
        b.clicked.connect(fn)
        self.focus.controls.addWidget(b)
        return b

    def mark(self, widget):
        self.cursor = QPointF(widget.mapTo(self.focus, widget.rect().center()))
        self.click_at = self.elapsed()

    def click(self, widget):
        self.mark(widget)
        widget.click()

    def combo(self, items, callback):
        c = QComboBox()
        c.addItems(items)
        c.currentIndexChanged.connect(callback)
        self.focus.controls.addWidget(c)
        return c

    def slider(self, maximum, callback):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(0, maximum)
        s.valueChanged.connect(callback)
        self.focus.controls.addWidget(s, 1)
        return s

    def cards_for_result(self, result):
        s = result["summary"]
        p = result["points"][0]
        return [
            ("期间供水", f"{s['delivered_m3']:.2f} m³", "本例计算时长 1 h"),
            ("最低末端压力", f"{p['min_pressure_m']:.2f} m", "要求 ≥15 m"),
            ("泵组电能", f"{s['energy_kwh']:.3f} kWh", "电输入口径"),
            ("未满足需求", f"{s['unmet_m3']:.3f} m³", "当前边界与负荷"),
        ]

    def intro(self):
        result = self.source["baseline"]
        self.main.display_result(result)
        f = self.open_focus(
            "把泵网运行，变成可见的决策依据",
            "FLOWSTATE / 离线泵—管网数字孪生工作台",
            self.cards_for_result(result),
            "实际软件窗口 · EPANET 计算示例 · 当前数据为示例模型，非现场设备承诺",
        )
        f.add_view(self.main.result_canvas, 3, borrow=True)
        f.add_view(self.main.trend, 2, borrow=True)
        self.main.result_canvas.set_dimension("3d")
        self.main.trend.key = "pump_curve"
        self.main.trend.update()
        self.button("供水与运行点", lambda: self.main.result_selection("link", "P1"))
        self.ready()
        self.dynamic = "intro"

    def design(self):
        self.main.set_document(self.base_doc.model_copy(deep=True), False)
        f = self.open_focus(
            "设计方案可以直接修改，也可以继续计算",
            "修改连接与参数，二维设计和三维空间检查共用同一工程",
            [
                ("自定义管网", "节点 / 支路", "自由连接、参数编辑"),
                ("空间检查", "二维 ↔ 三维", "保留高程与设备关系"),
                ("设计衔接", "CAD 兼容", "DXF中心线 / STEP、STL流体域"),
            ],
            "实机设计操作 · 这里聚焦编辑效果；CAD 与流体域导入细节留在使用文档中",
        )
        f.add_view(self.main.editor, 4, borrow=True)
        self.main.inspector_scroll.setMaximumWidth(320)
        f.add_view(self.main.inspector_scroll, 1, borrow=True)
        mode = self.combo(
            ["选择 / 移动", "新增节点", "连接管道"],
            lambda i: self.main.tool.setCurrentIndex(
                self.main.tool.findData(["select", "junction", "pipe"][i])
            ),
        )
        self.button("撤销", self.main.undo)
        self.button("重做", self.main.redo)
        self.view_button = self.button(
            "二维 / 三维",
            lambda: self.main.editor.set_dimension(
                "3d" if self.main.editor.dimension == "2d" else "2d"
            ),
        )
        self.main.editor.set_dimension("2d")
        self.main.select_element("link", "L3")
        self.main.inspector_scroll.verticalScrollBar().setValue(0)
        self.ready()

        def add():
            mode.setCurrentIndex(1)
            p = QPoint(int(self.main.editor.width() * 0.76), int(self.main.editor.height() * 0.23))
            QTest.mouseClick(self.main.editor, Qt.MouseButton.LeftButton, pos=p)
            self.new_id = self.main.selected[1]

        def connect():
            mode.setCurrentIndex(2)
            for key in ["J2", self.new_id]:
                n = next(n for n in self.main.document.nodes if n.id == key)
                p = self.main.editor.screen((n.x, n.y, n.z)).toPoint()
                QTest.mouseClick(self.main.editor, Qt.MouseButton.LeftButton, pos=p)
            self.main.inspector_scroll.verticalScrollBar().setValue(0)

        def diameter():
            self.main.fields["diameter_m"].setValue(0.055)
            self.main.apply_properties()
            self.main.inspector_scroll.verticalScrollBar().setValue(0)

        self.later(2, add)
        self.later(5, connect)
        self.later(7, diameter)
        self.later(9, self.main.undo)
        self.later(10, self.main.redo)
        self.later(11, lambda: (self.click(self.view_button), setattr(self, "dynamic", "design3d")))

    def operation(self):
        result = self.source["operating_replay"]
        self.main.display_result(result)
        f = self.open_focus(
            "转速变化，供水、压力与电功率联动显示",
            "用同一条时间轴观察运行效果，避免只看单一节能指标",
            self.cards_for_result(result),
            "真实 EPANET 结果回放 · 0.50 h：1.1→1.0 倍速；0.75 h：1.0→0.9 倍速 · 压力约束 15 m",
        )
        f.add_view(self.main.result_canvas, 3, borrow=True)
        f.add_view(self.main.trend, 2, borrow=True)
        self.main.result_canvas.set_dimension("2d")
        self.main.trend.key = "min_pressure_m"
        self.main.trend.label = "最低末端压力 / m"
        self.main.trend.update()
        choice = self.combo(
            ["节点压力", "管道流量"],
            lambda i: (
                setattr(self.main.result_canvas, "color_metric", ["pressure", "flow"][i]),
                self.main.result_canvas.update(),
            ),
        )
        self.timeline = self.slider(len(result["points"]) - 1, lambda i: self.operation_frame(i))
        self.dynamic = "operation"
        self.ready()
        self.later(12, lambda: choice.setCurrentIndex(1))
        self.later(16, lambda: choice.setCurrentIndex(0))

    def operation_frame(self, index):
        self.main.timeline.setValue(index)
        p = self.source["operating_replay"]["points"][index]
        self.focus.set_cards(
            [
                ("报告时刻", f"{p['hour']:.2f} h", "实际结果时间轴"),
                ("实际供水", f"{p['delivered_m3h']:.2f} m³/h", "目标 10.50 m³/h"),
                (
                    "最低末端压力",
                    f"{p['min_pressure_m']:.2f} m",
                    "达标" if p["min_pressure_m"] >= 15 else "低于15 m：需关注",
                ),
                ("电功率", f"{p['power_kw']:.3f} kW", "与供水约束共同判断"),
            ]
        )

    def optimization(self):
        d = self.source["optimization"]
        claim = self.source["customer_claims"]
        base = self.source["baseline"]
        ref = self.source["reference_high_speed"]
        f = self.open_focus(
            "有条件的节能，而非只挑最低功率",
            "在同一供水边界和时长下，对比可行方案",
            [
                ("泵组 / 转速组合", str(len(d["candidates"])) + " 种", "有限网格搜索"),
                ("本例电能减少", f"{claim['energy_saving_percent']:.1f}%", "1.1倍速 → 1.0倍速"),
                ("供水保持", f"{base['summary']['delivered_m3']:.2f} m³", "两方案差异小于0.01 m³"),
                ("额定转速末端压力", f"{claim['selected_min_pressure_m']:.2f} m", "高于15 m要求"),
            ],
            "本例对比，不是所有设备的保证收益 · NPSH约束尚未完成校核 · 不保证全局最优",
        )
        f.add_view(CandidateChart(d["candidates"]), 3)
        self.chart = DataChart(
            "同边界方案 · 电输入功率",
            [
                ("1.1倍速", [(p["hour"], p["power_kw"]) for p in ref["points"]], "#f0ba74"),
                ("1.0倍速", [(p["hour"], p["power_kw"]) for p in base["points"]], "#61dac1"),
            ],
            "时间 / h",
            "kW",
        )
        f.add_view(self.chart, 2)
        self.button("可行方案：单泵 1.0 倍速", lambda: self.mark(self.focus))
        self.ready()

    def flow(self):
        self.main.physics_panel.field_generation += 1
        f = self.open_focus(
            "把三维流动过程看清楚",
            "速度分布 → 压力切片与流线 → 汽蚀演化",
            [
                ("实际物理时段", "0—0.6 s", "全部61个输出时刻"),
                ("旋转工况", "600 rpm", "整周等效流量 5 m³/h"),
                ("数值示例", "30° 周期扇区", "480个体积单元"),
                ("计算位置", "本机", "实际求解器输出"),
            ],
            "周期扇区数值示例，非完整蜗壳、非实机标定 · 汽蚀低压工况统计尚未稳定",
        )
        self.field = FieldView()
        self.field.zoom = 1.48
        f.add_view(self.field, 3)
        self.replay_plot = SeriesView()
        f.add_view(self.replay_plot, 1)
        self.field_choice = self.combo(
            ["速度分布 + 方向", "压力切片 + 流线", "汽相率等值面 αv=0.1"], lambda i: None
        )
        self.flow_slider = self.slider(60, lambda i: None)
        self.dynamic = "flow"
        if self.flow_only:
            self.ready()
            return
        # Use absolute movie times, allowing setup and terminal-frame holds.
        for i in range(61):
            self.later(
                max(0, 71 + i * 12.8 / 60 - self.elapsed()),
                lambda i=i: self.show_flow("velocity", i),
            )
        for i in range(16):
            self.later(
                max(0, 84.3 + i * 0.37 - self.elapsed()), lambda i=i: self.show_flow("pressure", i)
            )
        for i in range(61):
            self.later(
                max(0, 91 + i * 12.8 / 60 - self.elapsed()), lambda i=i: self.show_flow("cavity", i)
            )
        self.ready()

    def show_flow(self, kind, index):
        if self.dynamic != "flow" or self.field is None:
            return
        d = self.replay[kind][index]
        self.current_replay = (kind, index)
        self.field.set_data(d)
        self.flow_slider.setValue(index)
        self.field_choice.setCurrentIndex(["velocity", "pressure", "cavity"].index(kind))
        if kind == "pressure":
            rows = [r for r in self.source["rotor"]["performance"] if r["seconds"] >= 0.15]
            self.replay_plot.set_series(
                [r["seconds"] for r in rows], [r["head_m"] for r in rows], "统计窗扬程 / m"
            )
        else:
            rows = self.replay[kind][: index + 1]
            self.replay_plot.set_series(
                [r["seconds"] for r in rows],
                [r["maximum"] for r in rows],
                "最大速度 / m·s⁻¹" if kind == "velocity" else "最大汽相率 / 1",
            )
        self.focus.subtitle.setText(
            {
                "velocity": "速度分布与方向 · 吸入绝压101.325 kPa",
                "pressure": "内部压力切片与真实流线 · t=0.6 s",
                "cavity": "汽相率等值面 αv=0.1 · 吸入绝压2.6 kPa · 统计尚未稳定",
            }[kind]
            + f" · t={d['seconds']:.3f} s"
        )
        self.actions.append(
            {
                "time": round(self.elapsed(), 3),
                "replay": kind,
                "index": index,
                "physical_time": d["seconds"],
            }
        )

    def loads(self):
        r = self.source["rotor"]
        rows = [x for x in r["loads"] if x["seconds"] >= 0.18]
        metrics = r["load_analysis"]["axes"]["force_x"]
        spectrum = [(x["hz"], x["amplitude"]) for x in metrics["spectrum"] if x["hz"] <= 100]
        f = self.open_focus(
            "从流场到载荷，比较脉动特征",
            "流体力时序与频谱同屏查看，不用在多个结果页之间来回寻找",
            [
                ("X向平均流体力", f"{metrics['mean']:.3f} N", "扇区统计窗"),
                ("脉动均方根", f"{metrics['rms_fluctuation']:.4f} N", "去除启动段"),
                ("峰峰值", f"{metrics['peak_to_peak']:.3f} N", "同一统计口径"),
                ("数据来源", "壁面载荷积分", "压力 + 黏性贡献"),
            ],
            "仅为周期扇区载荷，不能直接当作整泵径向力 · 频谱仅反映当前短时间窗",
        )
        self.chart = DataChart(
            "流体力时序",
            [("Fx", [(x["seconds"], x["force_n"][0]) for x in rows], "#62ddc0")],
            "时间 / s",
            "N",
        )
        f.add_view(self.chart, 1)
        f.add_view(
            DataChart("同一信号的频谱", [("幅值", spectrum, "#f2ba70")], "频率 / Hz", "N"), 1
        )
        self.button("力 / 力矩均可分析", lambda: None)
        self.dynamic = "loads"
        self.ready()

    def hammer(self):
        r = self.source["wave"]
        trace = r["pressure_probes"]
        peak = max(x["pressure_pa"][3] for x in trace) / 1e6
        f = self.open_focus(
            "先在模型里看清快速关阀冲击",
            "三维压力波与测点曲线保持同屏",
            [
                ("关阀情景", "快速关闭", "本机声学有限体积基准"),
                ("测点峰值", f"{peak:.3f} MPa", "当前算例绝对压力"),
                ("回放范围", "0—8 ms", "5个真实场输出"),
                ("管网瞬变", "支持停泵", "弹性管壁 / 转速惯性"),
            ],
            "三维声学小马赫数基准与一维弹性管网分开展示 · 不等同于结构应力、FSI或实机验证",
        )
        self.field = FieldView()
        self.field.zoom = 1.24
        for d in self.wave_frames:
            d["color_minimum"] = 101325
            d["color_maximum"] = r["summary"]["maximum_pressure_pa"]
        self.field.set_data(self.wave_frames[0])
        f.add_view(self.field, 1)
        self.chart = DataChart(
            "测点4 · 快速关阀压力响应",
            [
                (
                    "绝对压力",
                    [(x["seconds"] * 1000, x["pressure_pa"][3] / 1e6) for x in trace],
                    "#65ddc3",
                )
            ],
            "时间 / ms",
            "MPa",
        )
        f.add_view(self.chart, 1)
        self.dynamic = "hammer"
        self.ready()
        for i in range(5):
            self.later(i * 1.8, lambda i=i: self.field.set_data(self.wave_frames[i]))
        self.later(10, self.pump_trip)

    def pump_trip(self):
        r = self.source["transient"]
        rows = r["points"]
        node = "J3"
        start = rows[0]["nodes"][node]["pressure_pa"] / 1e6
        minimum = min(x["nodes"][node]["pressure_pa"] for x in rows) / 1e6
        f = self.open_focus(
            "停泵之后，压力与转速怎样变化？",
            "管网弹性瞬变 · 同步比较测点压力和设备转速",
            [
                ("事件发生", "0.010 s", "P1停泵"),
                ("J3初始绝压", f"{start:.3f} MPa", "同步节点压力"),
                ("J3最低绝压", f"{minimum:.3f} MPa", "当前0.1秒窗口"),
                ("泵转速响应", "惯性降速", "非瞬时置零"),
            ],
            "实际一维弹性 MOC 结果 · 当前时间窗0—0.1 s · 用于当前参数下的瞬态分析，非设备安全认证",
        )
        f.add_view(
            DataChart(
                "J3压力响应",
                [
                    (
                        "绝对压力",
                        [(x["seconds"], x["nodes"][node]["pressure_pa"] / 1e6) for x in rows],
                        "#68ddc4",
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
                        "#edb96e",
                    )
                ],
                "时间 / s",
                "n / n₀",
            ),
            1,
        )
        self.ready()

    def cavitation(self):
        r = self.source["cavitation"]
        points = r["map"]["points"]
        normal = points[0]["head_m"]
        low = points[-1]["head_m"]
        drop = 100 * (1 - low / normal)
        f = self.open_focus(
            "汽蚀不仅是云图，也要观察性能退化",
            "把吸入余量、扬程与结果可信状态放在一起",
            [
                ("非汽蚀参考扬程", f"{normal:.3f} m", "相同转速与流量"),
                ("低吸压扬程", f"{low:.3f} m", "当前统计窗均值"),
                ("本例下降趋势", f"{drop:.1f}%", "低压统计尚未稳定"),
                ("结论状态", "需继续计算", "不用于最终选型"),
            ],
            "实际OpenFOAM扫描输出 · 2.6 kPa工况统计半窗漂移超过2% · NPSH3与设备选型需更长计算和试验校核",
        )
        f.add_view(
            DataChart(
                "吸入余量与扬程 · 对数横轴",
                [
                    (
                        "扫描结果",
                        sorted([(math.log10(p["npsha_m"]), p["head_m"]) for p in points]),
                        "#68ddc3",
                    )
                ],
                "log₁₀(NPSHa / m)",
                "扬程 / m",
            ),
            1,
        )
        f.add_view(
            DataChart(
                "低压与非汽蚀工况的扬程响应",
                [
                    (
                        "非汽蚀",
                        [
                            (x["seconds"], x["head_m"])
                            for x in self.source["rotor"]["performance"]
                            if x["seconds"] >= 0.3
                        ],
                        "#62dec1",
                    ),
                    (
                        "低吸压",
                        [
                            (x["seconds"], x["head_m"])
                            for x in self.source["cavitating"]["performance"]
                            if x["seconds"] >= 0.3
                        ],
                        "#f0b46e",
                    ),
                ],
                "时间 / s",
                "扬程 / m",
            ),
            1,
        )
        self.ready()

    def calibration(self):
        c = self.source["calibration"]
        before = self.source["baseline"]
        after = self.source["calibrated_run"]
        obs = self.source["observations"]
        f = self.open_focus(
            "把模型和观测放在同一张图上",
            "偏差可量化，参数有依据地调整",
            [
                ("来源声明", "合成数据", "仅演示算法"),
                ("本次曲线倍率", f"{c['curve_factor']:.3f}", "仅拟合单泵曲线倍率"),
                ("训练 / 留出", f"{c['training_rows']} / {c['holdout_rows']}", "同一记录后30%留出"),
                ("下一步", "现场同步数据", "开展真实设备校准"),
            ],
            "当前并非传感器实测 · 留出记录不等于跨工况独立验证 · 不据此宣称实际设备预测精度",
        )
        f.add_view(
            DataChart(
                "J3压力：校准前 / 合成观测 / 校准后",
                [
                    (
                        "校准前",
                        [(p["hour"], p["nodes"]["J3"]["pressure_m"]) for p in before["points"]],
                        "#f0b875",
                    ),
                    ("合成观测", [(p["seconds"] / 3600, p["pressure_m"]) for p in obs], "#b7a5f1"),
                    (
                        "校准后",
                        [(p["hour"], p["nodes"]["J3"]["pressure_m"]) for p in after["points"]],
                        "#68dec2",
                    ),
                ],
                "时间 / h",
                "压力水头 / m",
            ),
            3,
        )
        f.add_view(
            DataChart(
                "拟合损失 · 前后对照",
                [
                    (
                        "校准前",
                        [(0, c["training_mse_before"]), (1, c["holdout_mse_before"])],
                        "#f0b875",
                    ),
                    (
                        "校准后",
                        [(0, c["training_mse_after"]), (1, c["holdout_mse_after"])],
                        "#68dec2",
                    ),
                ],
                "0=训练 / 1=留出",
                "归一化MSE",
            ),
            1,
        )
        self.ready()

    def delivery(self):
        r = self.source["coupled"]
        rows = r["history"]
        f = self.open_focus(
            "计算任务和校核状态，都能追踪",
            "统一调度、本机结果留存、双向耦合迭代记录",
            [
                ("求解位置", "本机独立", "无远程计算服务器"),
                ("双向耦合基准", f"{len(rows)} 次迭代", "当前数值基准已收敛"),
                ("末次流量残差", f"{rows[-1]['flow_relative_residual'] * 100:.2f}%", "当前阈值内"),
                ("结果交付", "工程 / CSV / JSON", "可复核、可继续分析"),
            ],
            "计算状态来自真实任务数据库 · 耦合为短时数值基准，不代表真实设备已校准",
        )
        self.main.refreshed((self.main.controller.snapshot(), self.main.controller.runs()))
        f.add_view(self.main.jobs_table, 3, borrow=True)
        self.main.jobs_table.setMinimumWidth(700)
        self.main.jobs_table.setColumnHidden(1, True)
        self.main.jobs_table.setColumnHidden(4, True)
        self.main.jobs_table.setColumnWidth(0, 300)
        self.main.jobs_table.setColumnWidth(2, 80)
        self.main.jobs_table.setColumnWidth(3, 300)
        f.add_view(
            DataChart(
                "双向耦合 · 流量相对残差",
                [
                    (
                        "相对残差",
                        [(x["iteration"], x["flow_relative_residual"]) for x in rows],
                        "#62ddc1",
                    )
                ],
                "迭代次数",
                "1",
            ),
            2,
        )
        self.button("暂停 / 恢复队列", self.main.pause_queue)
        self.ready()

    def ending(self):
        self.main.display_result(self.source["baseline"])
        f = self.open_focus(
            "FLOWSTATE / 设计 · 运行 · 风险分析",
            "让每次运行决策，都有可见的计算依据",
            [
                ("设计", "自由构建管网", "二维 / 三维 / CAD衔接"),
                ("运行", "供水与能耗联动", "比较可行的运行方案"),
                ("风险分析", "流场与瞬态回放", "汽蚀、载荷、停泵冲击"),
            ],
            "当前展示为示例模型与真实数值输出；项目应用需实际几何、同步试验数据及相应验证。",
        )
        f.add_view(self.main.result_canvas, 3, borrow=True)
        f.add_view(self.main.trend, 2, borrow=True)
        self.main.result_canvas.set_dimension("3d")
        self.main.trend.key = "pump_curve"
        self.main.trend.update()
        self.ready()
        self.dynamic = "ending"

    def check_jobs(self):
        jobs = self.main.controller.snapshot()["jobs"]
        self.actions.append(
            {
                "time": self.elapsed(),
                "jobs": [{"name": j["name"], "state": j["state"]} for j in jobs],
            }
        )
        if any(j["state"] == "failed" for j in jobs):
            self.errors.append({"error": "database contains failed task"})

    def verify_layout(self):
        if not self.focus:
            return
        area = self.focus.rect()
        bad = []
        # All principal views must be fully inside the visible, fixed-layout window.
        for i in range(self.focus.body.count()):
            w = self.focus.body.itemAt(i).widget()
            if w is None:
                continue
            r = w.geometry()
            if not area.contains(r):
                bad.append((type(w).__name__, r.getRect(), area.getRect()))
            if r.width() < 250 or r.height() < 200 or not w.isVisible():
                bad.append((type(w).__name__, "principal view missing or too small", r.getRect()))
        if self.focus.height() > self.app.primaryScreen().availableGeometry().height():
            bad.append("window exceeds screen")
        if bad:
            self.bad_layouts.append({"time": self.elapsed(), "violations": bad})

    def evidence(self, index):
        self.verify_layout()
        self.actions.append(
            {
                "time": self.elapsed(),
                "layout": index,
                "window": self.focus.size().toTuple(),
                "views": [
                    {
                        "class": type(self.focus.body.itemAt(i).widget()).__name__,
                        "geometry": self.focus.body.itemAt(i).widget().geometry().getRect(),
                        "visible": self.focus.body.itemAt(i).widget().isVisible(),
                    }
                    for i in range(self.focus.body.count())
                    if self.focus.body.itemAt(i).widget() is not None
                ],
            }
        )
        self.render(self.elapsed()).save(str(OUT / "frames" / f"chapter-{index:02}.png"))

    def update_motion(self, t):
        if self.dynamic in ["intro", "ending", "design3d"]:
            c = self.main.editor if self.dynamic == "design3d" else self.main.result_canvas
            c.yaw = 0.45 + (0.035 * t)
            c.pitch = 0.35 + 0.12 * math.sin(t * 0.12)
            c._view_scale = None
            c.update()
        if self.dynamic == "operation":
            index = min(12, int(max(0, t - 29) / 18 * 13))
            if self.timeline.value() != index:
                self.timeline.setValue(index)
        if self.dynamic == "flow" and self.field:
            self.field.yaw = -0.7 + (t - 70) * 0.031
            self.field.pitch = 0.45 + 0.12 * math.sin((t - 70) * 0.13)
            self.field.update()
        if self.dynamic == "loads" and self.chart:
            self.chart.cursor = 0.18 + 0.42 * min(1, max(0, (t - 105) / 14))
            self.chart.update()

    def render(self, t):
        image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#0c1823"))
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        scene = next((s for s in self.story if s["start"] <= t < s["end"]), self.story[-1])
        p.fillRect(0, 0, WIDTH, 5, QColor("#59d6b6"))
        p.setFont(QFont("PingFang SC", 23, QFont.Weight.DemiBold))
        p.setPen(QColor("#eef8f4"))
        p.drawText(42, 45, "FLOWSTATE  /  " + scene["title"])
        p.setFont(QFont("PingFang SC", 13))
        p.setPen(QColor("#a3c8ba"))
        p.drawText(
            QRectF(1340, 14, 535, 40),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"真实软件结果演示   {int(t) // 60:02}:{int(t) % 60:02} / 03:00",
        )
        if self.focus:
            raw = self.focus.grab().toImage()
            available = QRectF(40, 77, 1840, 900)
            scale = min(
                available.width() / self.focus.width(), available.height() / self.focus.height()
            )
            w = self.focus.width() * scale
            h = self.focus.height() * scale
            dest = QRectF(available.center().x() - w / 2, available.center().y() - h / 2, w, h)
            p.drawImage(dest, raw)
            if self.cursor is not None:
                pos = QPointF(
                    dest.x() + self.cursor.x() * scale, dest.y() + self.cursor.y() * scale
                )
                age = t - self.click_at
                if 0 <= age < 0.8:
                    p.setPen(QPen(QColor("#f0ca70"), 3))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawEllipse(pos, 20 + age * 15, 20 + age * 15)
        p.fillRect(0, 991, WIDTH, 89, QColor("#0c1823"))
        p.setFont(QFont("PingFang SC", 23, QFont.Weight.Medium))
        p.setPen(QColor("#f0f8f5"))
        text = next((text for start, end, text in self.cues if start <= t < end), "")
        p.drawText(
            QRectF(60, 994, 1800, 74), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text
        )
        p.fillRect(0, 1076, round(WIDTH * min(t / 180, 1)), 4, QColor("#59d6b6"))
        p.end()
        return image

    def capture(self):
        if self.flow_only:
            if self.frames >= self.duration * FPS:
                self.finish()
                return
            if self.dynamic != "flow":
                return
            # Step the real GUI by output frame. This prevents timer coalescing
            # from skipping a physical result on a slower desktop render.
            t = self.offset + self.frames / FPS
            if t < 84.3:
                kind, index = "velocity", min(60, max(0, int((t - 71) / 12.8 * 60)))
            elif t < 91:
                kind, index = "pressure", min(15, max(0, int((t - 84.3) / 0.37)))
            else:
                kind, index = "cavity", min(60, max(0, int((t - 91) / 12.8 * 60)))
            if self.current_replay != (kind, index):
                self.show_flow(kind, index)
            self.update_motion(t)
            frame = self.render(t)
            self.seen.add((kind, index))
            self.work_queue.put(bytes(frame.constBits()))
            self.frames += 1
            return
        t = self.elapsed()
        if t >= self.offset + self.duration:
            return
        self.update_motion(t)
        if self.rehearse:
            return
        frame = self.render(t)
        target = min(self.duration * FPS, math.floor((t - self.offset) * FPS) + 1)
        if self.current_replay and self.frames < target:
            self.seen.add(self.current_replay)
        raw = bytes(frame.constBits())
        while self.frames < target:
            self.work_queue.put(raw)
            self.frames += 1

    def finish(self):
        self.timer.stop()
        self.verify_layout()
        if not self.rehearse:
            raw = bytes(self.render(self.offset + self.duration - 0.01).constBits())
            while self.frames < self.duration * FPS:
                self.work_queue.put(raw)
                self.frames += 1
            self.work_queue.put(None)
            self.writer.join(timeout=90)
            if self.writer_error:
                self.errors.append({"error": self.writer_error})
            if self.process.poll() is None:
                self.process.kill()
                self.errors.append({"error": "encoder timeout"})
        if self.focus:
            self.focus.close()
        self.main.dirty = False
        self.main.snapshot = {"jobs": []}
        self.main.close()
        self.app.processEvents()
        report = {
            "duration_s": self.duration,
            "movie_offset_s": self.offset,
            "workspace": str(self.take),
            "fps": FPS,
            "frames": self.frames,
            "rehearsal": self.rehearse,
            "qt_platform": self.app.platformName(),
            "visible_native_window": True,
            "new_capture": True,
            "old_media_reused": False,
            "capture": "Actual running FLOWSTATE native ResultFocus / original editable and result widgets",
            "replay_capture_mode": "native GUI frame-stepped at output 20 fps"
            if self.flow_only
            else "wall-clock GUI capture",
            "scroll_policy": "Principal views fixed side-by-side; no main page scrolling",
            "layout_violations": self.bad_layouts,
            "errors": self.errors,
            "captured_replay_indices": {
                kind: sorted(i for k, i in self.seen if k == kind)
                for kind in ["velocity", "pressure", "cavity"]
            },
            "actions": self.actions,
        }
        name = (
            "flow_recording.json"
            if self.flow_only
            else ("rehearsal.json" if self.rehearse else "recording.json")
        )
        (OUT / name).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("REHEARSAL READY" if self.rehearse else "RECORDING READY", flush=True)
        self.app.quit()


def compose():
    story = json.loads((OUT / "storyboard.json").read_text())
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    raw = OUT / "新录屏_无配音.mp4"
    flow_log = OUT / "flow_recording.json"
    if flow_log.exists():
        patch = json.loads(flow_log.read_text())
        assert patch["frames"] == 700 and not patch["errors"] and not patch["layout_violations"]
        assert {k: len(v) for k, v in patch["captured_replay_indices"].items()} == {
            "velocity": 61,
            "pressure": 16,
            "cavity": 61,
        }
        joined = OUT / "新录屏_合并无配音.mp4"
        subprocess.run(
            [
                ff,
                "-y",
                "-v",
                "error",
                "-i",
                str(raw),
                "-i",
                str(OUT / "三维补录_无配音.mp4"),
                "-filter_complex",
                "[0:v]trim=start=0:end=70,setpts=PTS-STARTPTS[a];"
                "[1:v]setpts=PTS-STARTPTS[b];"
                "[0:v]trim=start=105:end=180,setpts=PTS-STARTPTS[c];"
                "[a][b][c]concat=n=3:v=1:a=0[v]",
                "-map",
                "[v]",
                "-an",
                "-r",
                "20",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "17",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(joined),
            ],
            check=True,
        )
        (OUT / "edit_log.json").write_text(
            json.dumps(
                {
                    "duration_s": 180,
                    "segments": [
                        {"source": raw.name, "source_s": [0, 70], "movie_s": [0, 70]},
                        {
                            "source": "三维补录_无配音.mp4",
                            "source_s": [0, 35],
                            "movie_s": [70, 105],
                            "capture_log": flow_log.name,
                        },
                        {"source": raw.name, "source_s": [105, 180], "movie_s": [105, 180]},
                    ],
                    "reason": "补齐并延长保留0.6秒终帧；所有素材均为本次新录，不使用旧版媒体",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raw = joined
    args = [ff, "-y", "-v", "warning", "-i", str(raw)]
    for s in story:
        args += ["-i", s["audio"]]
    filters = [
        f"[{i + 1}:a]adelay={round((s['start'] + 0.3) * 1000)}:all=1[a{i}]"
        for i, s in enumerate(story)
    ]
    filters.append(
        "".join(f"[a{i}]" for i in range(len(story)))
        + f"amix=inputs={len(story)}:duration=longest:normalize=0,apad,atrim=duration=180,loudnorm=I=-16:TP=-1.5:LRA=9[a]"
    )
    args += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "copy",
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
            f"{i + 1}\n{stamp(a)} --> {stamp(b)}\n{text}"
            for i, (a, b, text) in enumerate(captions(story))
        )
        + "\n"
    )
    (OUT / "配音与章节.md").write_text(
        "# FLOWSTATE 客户演示 · 3分钟\n\n全新录屏与配音；以客户任务和结果为主线，导入细节不占用演示时长。\n\n"
        + "\n\n".join(
            f"## {s['start'] // 60:02}:{s['start'] % 60:02}—{s['end'] // 60:02}:{s['end'] % 60:02} {s['title']}\n\n{s['text']}"
            for s in story
        )
    )
    clip = OUT / "FLOWSTATE_三维结果专段_35秒.mp4"
    subprocess.run(
        [
            ff,
            "-y",
            "-v",
            "error",
            "-ss",
            "70",
            "-i",
            str(FINAL),
            "-t",
            "35",
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
            "-movflags",
            "+faststart",
            str(clip),
        ],
        check=True,
    )
    print("COMPOSED", FINAL, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rehearse", action="store_true")
    parser.add_argument("--compose", action="store_true")
    parser.add_argument("--flow-only", action="store_true")
    args = parser.parse_args()
    if args.compose:
        compose()
    else:
        prepare_qt_plugins()
        app = QApplication([sys.argv[0]])
        app.setFont(QFont("PingFang SC", 12))
        recorder = Recorder(app, args.rehearse, args.flow_only)
        app.exec()
        if recorder.errors or recorder.bad_layouts:
            raise SystemExit(1)
