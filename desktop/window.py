import csv
import json
import math
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from backend.models import DesignLink, DesignNode, DesignSettings, NetworkDocument, default_document
from backend.physics import export_design_inp, validate_topology
from .canvas import NetworkCanvas, TrendWidget
from .controller import Controller

STATE_NAMES = {
    "queued": "等待",
    "running": "执行中",
    "cancelling": "等待取消",
    "succeeded": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}
KIND_NAMES = {
    "design_simulation": "自建管网仿真",
    "design_optimization": "自建管网优化",
    "design_calibration": "测量数据校准",
}
NODE_NAMES = {"junction": "连接/需水节点", "reservoir": "定水头水源", "tank": "水箱"}
LINK_NAMES = {"pipe": "管道", "pump": "离心泵", "valve": "调节阀"}
LABELS = {
    "x": "X 坐标 / m",
    "y": "Y 坐标 / m",
    "z": "Z 几何坐标 / m",
    "elevation_m": "水力高程 / m",
    "demand_m3h": "期望用水量 / m³/h",
    "source_head_m": "水源总水头 / m",
    "tank_level_m": "初始液位 / m",
    "tank_min_m": "最低液位 / m",
    "tank_max_m": "最高液位 / m",
    "tank_diameter_m": "水箱内径 / m",
    "length_m": "实际管长 / m",
    "diameter_m": "管道/阀门内径 / m",
    "roughness_m": "绝对粗糙度 / m",
    "minor_loss": "局部阻力系数 K",
    "opening": "阀门开度 0–1",
    "speed_ratio": "转速比",
    "nominal_rpm": "参考转速 / rpm",
    "motor_efficiency": "电机效率",
    "drive_efficiency": "驱动效率",
    "motor_rated_kw": "额定轴功率 / kW",
    "duration_hours": "仿真时长 / h",
    "step_seconds": "报告步长 / s",
    "demand_multiplier": "用水量倍数",
    "required_pressure_m": "所需末端压力 / m",
    "temperature_c": "清水温度 / ℃",
    "atmospheric_kpa": "大气绝压 / kPa",
    "electricity_price": "电价 / 元/kWh",
}


def numeric(value):
    w = QDoubleSpinBox()
    w.setDecimals(6)
    w.setRange(-1e8, 1e8)
    w.setValue(float(value))
    w.setSingleStep(0.1)
    w.setKeyboardTracking(False)
    w.setMinimumHeight(30)
    w.setMinimumWidth(110)
    w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    return w


def button(text, callback):
    w = QPushButton(text)
    w.clicked.connect(callback)
    return w


class WorkerSignals(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)


class Worker(QRunnable):
    def __init__(self, key, fn):
        super().__init__()
        self.key = key
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.done.emit(self.key, self.fn())
        except Exception as exc:
            self.signals.failed.emit(self.key, str(exc))


class MainWindow(QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.setWindowTitle("FLOWSTATE · 二维/三维管网设计与数字孪生")
        self.resize(1440, 920)
        self.document = default_document()
        self.file_path = None
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.selected = None
        self.fields = {}
        self.current_result = None
        self.current_run_id = None
        self.results = {}
        self.snapshot = {"jobs": [], "counts": {}, "paused": False}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.callbacks = {}
        self.workers = {}
        self.polling = False
        self.closing = False
        self.controller = Controller(data_dir)
        self._build()
        self._style()
        self._refresh_title()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh)
        self.poll_timer.start(1500)
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self.advance_frame)
        self.refresh()

    def _build(self):
        toolbar = QToolBar("项目")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for name, callback, shortcut in [
            ("新建", self.new_project, QKeySequence.StandardKey.New),
            ("打开", self.open_project, QKeySequence.StandardKey.Open),
            ("保存", self.save_project, QKeySequence.StandardKey.Save),
            ("另存为", lambda: self.save_project(True), QKeySequence.StandardKey.SaveAs),
            ("撤销", self.undo, QKeySequence.StandardKey.Undo),
            ("重做", self.redo, QKeySequence.StandardKey.Redo),
        ]:
            action = QAction(name, self)
            action.triggered.connect(callback)
            action.setShortcut(shortcut)
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction("导入 CAD / DXF", self.import_cad)
        toolbar.addAction("导出 INP", self.export_inp)
        toolbar.addAction("分割所选管段", self.split_selected_pipe)
        toolbar.addSeparator()
        toolbar.addAction("仿真设置", self.edit_settings)
        toolbar.addAction("校验管网", self.validate_document)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(12)
        self.setCentralWidget(container)
        header = QHBoxLayout()
        brand = QLabel("FLOWSTATE  /  管网设计与数字孪生")
        brand.setObjectName("brand")
        header.addWidget(brand)
        header.addStretch()
        self.connection_label = QLabel("本机独立计算 · 数据保存在本机")
        header.addWidget(self.connection_label)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        design = QWidget()
        designlayout = QVBoxLayout(design)
        designlayout.setContentsMargins(0, 9, 0, 0)
        tools = QHBoxLayout()
        self.tool = QComboBox()
        for text, value in [
            ("选择/移动", "select"),
            ("放置需水节点", "junction"),
            ("放置水源", "reservoir"),
            ("放置水箱", "tank"),
            ("连接管道", "pipe"),
            ("连接离心泵", "pump"),
            ("连接调节阀", "valve"),
        ]:
            self.tool.addItem(text, value)
        self.tool.currentIndexChanged.connect(self.change_tool)
        tools.addWidget(self.tool)
        self.view_mode = QComboBox()
        self.view_mode.addItem("二维 XY 编辑", "2d")
        self.view_mode.addItem("三维旋转视图", "3d")
        self.view_mode.currentIndexChanged.connect(
            lambda: self.editor.set_dimension(self.view_mode.currentData())
        )
        tools.addWidget(self.view_mode)
        tools.addWidget(button("适应视图", lambda: self.editor.fit()))
        tools.addWidget(button("删除所选", self.delete_selection))
        tools.addWidget(button("将 Z 映射为水力高程", self.map_elevation))
        tools.addStretch()
        tools.addWidget(button("加入仿真队列", self.submit_simulation))
        tools.addWidget(button("搜索低能耗方案", self.submit_optimization))
        designlayout.addLayout(tools)
        split = QSplitter()
        self.editor = NetworkCanvas(self.document)
        split.addWidget(self.editor)
        self.editor.selected.connect(self.select_element)
        self.editor.add_requested.connect(self.add_node)
        self.editor.connect_requested.connect(self.add_link)
        self.editor.begin_edit.connect(self.checkpoint)
        self.editor.changed.connect(self.canvas_changed)
        self.editor.delete_requested.connect(self.delete_selection)
        self.inspector = QWidget()
        self.inspector_layout = QVBoxLayout(self.inspector)
        self.inspector_layout.setContentsMargins(14, 8, 8, 8)
        self.inspector_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.inspector)
        scroll.setMinimumWidth(380)
        scroll.setMaximumWidth(450)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        designlayout.addWidget(split, 1)
        hint = QLabel(
            "放置节点后，用连接工具依次点击两端。三维视图拖动旋转；二维视图拖动节点。绘图坐标、高程、管长分别可编辑。"
        )
        hint.setWordWrap(True)
        designlayout.addWidget(hint)
        self.tabs.addTab(design, "二维/三维管网设计")
        self.show_inspector()
        self._build_results()
        self._build_dispatch()
        self._build_data()
        self._build_observations()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(100)
        layout.addWidget(self.log)
        self.statusBar().showMessage(
            "全部演示参数尚未完成实机验证；CAD 中心线导入后需补充水力边界。"
        )

    def _build_results(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 10, 0, 0)
        row = QHBoxLayout()
        self.history = QComboBox()
        self.history.setMinimumWidth(260)
        row.addWidget(self.history, 1)
        row.addWidget(button("读取历史结果", self.load_selected_history))
        row.addWidget(button("将结果管网作为新草稿", self.use_result_document))
        row.addWidget(button("导出结果 JSON", self.export_result))
        row.addWidget(button("导出时间序列 CSV", self.export_csv))
        v.addLayout(row)
        row.addWidget(button("与当前结果比较", self.compare_selected_history))
        self.result_summary = QLabel("计算完成后，任务队列中选择任务并点击“查看结果”。")
        self.result_summary.setObjectName("summary")
        self.result_summary.setWordWrap(True)
        v.addWidget(self.result_summary)
        self.result_canvas = NetworkCanvas(default_document(), editable=False)
        self.result_canvas.set_dimension("3d")
        self.result_canvas.selected.connect(self.result_selection)
        v.addWidget(self.result_canvas, 1)
        row = QHBoxLayout()
        row.addWidget(
            button(
                "二维 / 三维",
                lambda: self.result_canvas.set_dimension(
                    "2d" if self.result_canvas.dimension == "3d" else "3d"
                ),
            )
        )
        self.color_mode = QComboBox()
        self.color_mode.addItem("节点压力", "pressure")
        self.color_mode.addItem("管道流量", "flow")
        self.color_mode.currentIndexChanged.connect(self.change_result_color)
        row.addWidget(self.color_mode)
        self.play_button = button("播放", self.toggle_play)
        row.addWidget(self.play_button)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.valueChanged.connect(self.show_frame)
        row.addWidget(self.timeline, 1)
        self.time_label = QLabel("0.00 h")
        row.addWidget(self.time_label)
        v.addLayout(row)
        self.trend_metric = QComboBox()
        self.trend_metric.addItem("电功率 / kW", "power_kw")
        self.trend_metric.addItem("实际供水 / m³/h", "delivered_m3h")
        self.trend_metric.addItem("未满足需求 / m³/h", "unmet_m3h")
        self.trend_metric.addItem("最低末端压力 / m", "min_pressure_m")
        self.trend_metric.addItem("所选泵 Q–H 性能曲线", "pump_curve")
        self.trend_metric.currentIndexChanged.connect(self.change_metric)
        v.addWidget(self.trend_metric)
        self.trend = TrendWidget()
        v.addWidget(self.trend)
        self.tabs.addTab(page, "结果可视化与回放")

    def _build_dispatch(self):
        page = QWidget()
        v = QVBoxLayout(page)
        row = QHBoxLayout()
        self.queue_summary = QLabel("计算引擎就绪")
        row.addWidget(self.queue_summary, 1)
        self.pause_button = button("暂停队列", self.pause_queue)
        row.addWidget(self.pause_button)
        row.addWidget(button("刷新", self.refresh))
        row.addWidget(button("取消所选任务", self.cancel_selected_job))
        row.addWidget(button("查看任务结果", self.open_job_result))
        v.addLayout(row)
        self.jobs_table = QTableWidget(0, 5)
        self.jobs_table.setHorizontalHeaderLabels(
            ["任务名称", "类型", "状态", "执行阶段", "提交时间"]
        )
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.horizontalHeader().setStretchLastSection(True)
        self.jobs_table.itemDoubleClicked.connect(self.open_job_result)
        v.addWidget(self.jobs_table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("批量转速比："))
        self.batch_values = QLineEdit("0.85, 0.95, 1.05")
        row.addWidget(self.batch_values, 1)
        row.addWidget(button("创建批量仿真", self.submit_batch))
        v.addLayout(row)
        label = QLabel("暂停只阻止启动后续任务；原生水力调用不能即时中断，取消将在返回后生效。")
        label.setWordWrap(True)
        v.addWidget(label)
        self.tabs.addTab(page, "统一任务调度")

    def _build_data(self):
        page = QWidget()
        v = QVBoxLayout(page)
        text = QLabel(
            "工程文件使用可移植 JSON，包含二维/三维坐标、设备参数、控制事件与 CAD 来源。CAD 仅识别中心线，DWG 请先另存为 DXF；不会从三维实体外观自动识别管径或泵。"
        )
        text.setWordWrap(True)
        v.addWidget(text)
        row = QHBoxLayout()
        row.addWidget(button("查看当前工程 JSON", self.inspect_json))
        row.addWidget(button("从 JSON 编辑工程", self.edit_json))
        row.addWidget(button("导入结果 JSON", self.import_result))
        row.addWidget(button("查看 CAD 导入报告", self.show_cad_report))
        v.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        v.addWidget(self.details, 1)
        docs = QLabel(
            "二维/三维是同一管网的不同视图。水力求解为清水准稳态 EPANET 2.2 / Darcy–Weisbach / PDD，不包含水锤、机械振动和两相流。所有计算在本机执行。"
        )
        docs.setWordWrap(True)
        v.addWidget(docs)
        self.tabs.addTab(page, "工程与数据")

    def _build_observations(self):
        page = QWidget()
        v = QVBoxLayout(page)
        label = QLabel(
            "观测 CSV 列：timestamp（含时区）、pressure_m、flow_m3h、power_kw。压力对应所选节点，流量对应所选管段/泵，功率为全泵组电输入。首条记录对应 t=0，仅精确匹配报告时刻。"
        )
        label.setWordWrap(True)
        v.addWidget(label)
        form = QFormLayout()
        self.observation_node = QComboBox()
        self.observation_link = QComboBox()
        self.calibration_pump = QComboBox()
        form.addRow("压力测点节点", self.observation_node)
        form.addRow("流量测点设备", self.observation_link)
        form.addRow("待校准泵", self.calibration_pump)
        self.observation_source = QComboBox()
        self.observation_source.addItem("导入者声明为实测", "measured")
        self.observation_source.addItem("合成数据 / 演示", "synthetic")
        form.addRow("数据来源", self.observation_source)
        self.observation_note = QLineEdit()
        self.observation_note.setPlaceholderText("设备、采集日期与数据来源")
        form.addRow("来源说明", self.observation_note)
        v.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(button("读取观测 CSV", self.import_observation_csv))
        row.addWidget(button("与当前结果对照", self.compare_observations))
        row.addWidget(button("加入单泵曲线校准队列", self.submit_calibration))
        row.addWidget(button("应用最近校准为草稿", self.apply_calibration))
        v.addLayout(row)
        self.observation_text = QPlainTextEdit()
        self.observation_text.setPlaceholderText(
            "timestamp,pressure_m,flow_m3h,power_kw\n2026-11-03T06:00:00+08:00,17.0,10.5,1.29"
        )
        v.addWidget(self.observation_text, 1)
        self.calibration_output = QPlainTextEdit()
        self.calibration_output.setReadOnly(True)
        v.addWidget(self.calibration_output, 1)
        self.last_calibration = None
        self.tabs.addTab(page, "观测对照与校准")
        self.update_observation_targets()

    def update_observation_targets(self):
        if not hasattr(self, "observation_node"):
            return
        previous_node = self.observation_node.currentText()
        previous_link = self.observation_link.currentText()
        for combo, values in [
            (self.observation_node, [n.id for n in self.document.nodes]),
            (self.observation_link, [l.id for l in self.document.links]),
            (self.calibration_pump, [l.id for l in self.document.links if l.kind == "pump"]),
        ]:
            selected = combo.currentText()
            combo.clear()
            combo.addItems(values)
            if selected in values:
                combo.setCurrentText(selected)
        if previous_node not in [n.id for n in self.document.nodes]:
            demand = next((n.id for n in self.document.nodes if n.demand_m3h > 0), None)
            if demand:
                self.observation_node.setCurrentText(demand)
        if (
            previous_link not in [l.id for l in self.document.links]
            and self.calibration_pump.count()
        ):
            self.observation_link.setCurrentText(self.calibration_pump.currentText())

    def import_observation_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入观测 CSV", "", "CSV (*.csv)")
        if path:
            try:
                self.observation_text.setPlainText(Path(path).read_text(encoding="utf-8-sig"))
                self.observation_note.setText(Path(path).name)
            except Exception as exc:
                self.show_error(str(exc))

    def compare_observations(self):
        if not self.current_result:
            self.show_error("请先加载一个计算结果")
            return
        try:
            from backend.observations import parse_observations, observation_report

            report = observation_report(
                self.current_result,
                parse_observations(self.observation_text.toPlainText()),
                self.observation_node.currentText(),
                self.observation_link.currentText(),
            )
            report["declared_source"] = self.observation_source.currentData()
            self.calibration_output.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.show_error(str(exc))

    def submit_calibration(self):
        try:
            from backend.observations import parse_observations

            text = self.observation_text.toPlainText()
            parse_observations(text)
            if not self.observation_note.text().strip():
                raise ValueError("请填写来源说明")
            options = {
                "observation_csv": text,
                "measurement_node": self.observation_node.currentText(),
                "measurement_link": self.observation_link.currentText(),
                "calibration_pump": self.calibration_pump.currentText(),
                "observation_source": self.observation_source.currentData(),
                "source_note": self.observation_note.text(),
            }
            controller = self.controller
            document = self.document.model_dump(mode="json")
            self.work(lambda: controller.calibrate(document, options), self.task_submitted)
        except Exception as exc:
            self.show_error(str(exc))

    def display_calibration(self, value):
        self.last_calibration = value
        self.calibration_output.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
        self.tabs.setCurrentIndex(4)

    def apply_calibration(self):
        if self.last_calibration and self.confirm_discard():
            self.checkpoint()
            self.set_document(NetworkDocument.model_validate(self.last_calibration["document"]))
            self.tabs.setCurrentIndex(0)

    def _style(self):
        self.setStyleSheet(
            """QMainWindow,QDialog{background:#f2f6f3;color:#234539} QWidget{font-size:12px;} QLabel{color:#627e70;} QLabel#brand{font-size:21px;font-weight:600;color:#214f3d;padding:5px;} QLabel#summary{background:#e8f2e9;color:#335d43;padding:13px;border-radius:6px;} QPushButton{background:#fff;border:1px solid #cdded2;border-radius:5px;padding:7px 12px;color:#39684b;} QPushButton:hover{background:#e5f0e6;} QPushButton:disabled{color:#acb9b0;} QLineEdit,QDoubleSpinBox,QSpinBox,QComboBox,QPlainTextEdit{background:#fff;border:1px solid #d6e3d8;border-radius:4px;padding:5px;color:#355d43;selection-background-color:#b5d2bb;} QTabWidget::pane{border:1px solid #dbe6dc;border-radius:5px;background:#fff;} QTabBar::tab{padding:10px 19px;color:#7c9585;background:#eaf0e8;} QTabBar::tab:selected{background:white;color:#326648;} QToolBar{background:#fff;border-bottom:1px solid #dce6db;padding:6px;spacing:6px;} QTableWidget{background:white;border:1px solid #dce7db;gridline-color:#e7eee4;} QHeaderView::section{background:#eef4e9;padding:8px;border:0;color:#708766;} QProgressBar{border:0;background:#e9f0e6;border-radius:3px;text-align:center;height:14px;font-size:10px;} QProgressBar::chunk{background:#94bb92;border-radius:3px;} QScrollArea{border:0;} QSplitter::handle{background:#e8eee6;}"""
        )

    def work(self, fn, done=None, failed=None):
        key = uuid.uuid4().hex
        worker = Worker(key, fn)
        self.callbacks[key] = (done, failed)
        self.workers[key] = worker
        worker.signals.done.connect(self._work_done)
        worker.signals.failed.connect(self._work_failed)
        self.pool.start(worker)
        return key

    @Slot(str, object)
    def _work_done(self, key, value):
        callbacks = self.callbacks.pop(key, (None, None))
        self.workers.pop(key, None)
        if callbacks[0] and not self.closing:
            try:
                callbacks[0](value)
            except Exception as exc:
                self.show_error(str(exc))

    @Slot(str, str)
    def _work_failed(self, key, error):
        callbacks = self.callbacks.pop(key, (None, None))
        self.workers.pop(key, None)
        if self.closing:
            return
        if callbacks[1]:
            callbacks[1](error)
        else:
            self.show_error(error)

    def show_error(self, message):
        self.log.appendPlainText("错误：" + message)
        self.statusBar().showMessage(message, 15000)

    def _refresh_title(self):
        self.setWindowTitle(f"FLOWSTATE · {self.document.name}" + (" *" if self.dirty else ""))

    def checkpoint(self):
        self.undo_stack.append(self.document.model_dump_json())
        self.undo_stack = self.undo_stack[-60:]
        self.redo_stack.clear()

    def modified(self):
        self.dirty = True
        self.editor.reindex()
        self.editor.update()
        self._refresh_title()
        self.update_observation_targets()

    def canvas_changed(self):
        self.modified()
        self.show_inspector()

    def set_document(self, doc, dirty=True, path=None):
        self.document = doc
        self.file_path = path
        self.dirty = dirty
        self.selected = None
        self.editor.set_document(doc)
        self.show_inspector()
        self._refresh_title()
        self.update_observation_targets()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.document.model_dump_json())
            self.set_document(
                NetworkDocument.model_validate_json(self.undo_stack.pop()), True, self.file_path
            )

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.document.model_dump_json())
            self.set_document(
                NetworkDocument.model_validate_json(self.redo_stack.pop()), True, self.file_path
            )

    def confirm_discard(self):
        return (
            not self.dirty
            or QMessageBox.question(
                self,
                "未保存工程",
                "当前修改尚未保存，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def new_project(self):
        if not self.confirm_discard():
            return
        self.checkpoint()
        self.set_document(NetworkDocument(name="新建管网"))

    def open_project(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开管网工程", "", "管网 JSON (*.json)")
        if path:
            self.load_project(Path(path))

    def load_project(self, path):
        try:
            doc = NetworkDocument.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
            self.checkpoint()
            self.set_document(doc, False, Path(path))
            self.log.appendPlainText("已打开工程：" + str(path))
        except Exception as exc:
            self.show_error(str(exc))

    def save_project(self, save_as=False):
        path = self.file_path
        if save_as or not path:
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "保存管网工程",
                str(path or self.document.name + ".json"),
                "管网 JSON (*.json)",
            )
            if not selected:
                return
            path = Path(selected)
        try:
            validated = NetworkDocument.model_validate(self.document.model_dump())
            path.write_text(validated.model_dump_json(indent=2), encoding="utf-8")
            self.file_path = path
            self.dirty = False
            self._refresh_title()
            self.log.appendPlainText("已保存：" + str(path))
        except Exception as exc:
            self.show_error(str(exc))

    def change_tool(self):
        self.editor.mode = self.tool.currentData()
        self.editor.connection_start = None
        self.editor.update()

    def next_id(self, prefix, items):
        used = {i.id for i in items}
        i = 1
        while prefix + str(i) in used:
            i += 1
        return prefix + str(i)

    @Slot(str, float, float)
    def add_node(self, kind, x, y):
        self.checkpoint()
        identifier = self.next_id(
            {"junction": "J", "reservoir": "R", "tank": "T"}[kind], self.document.nodes
        )
        n = DesignNode(
            id=identifier,
            kind=kind,
            x=x,
            y=y,
            demand_m3h=1 if kind == "junction" else 0,
            source_head_m=25 if kind == "reservoir" else 0,
        )
        self.document.nodes.append(n)
        self.modified()
        self.select_element("node", identifier)

    @Slot(str, str, str)
    def add_link(self, kind, start, end):
        self.checkpoint()
        identifier = self.next_id(
            {"pipe": "L", "pump": "P", "valve": "V"}[kind], self.document.links
        )
        nodes = {n.id: n for n in self.document.nodes}
        a, b = nodes[start], nodes[end]
        link = DesignLink(
            id=identifier,
            kind=kind,
            start=start,
            end=end,
            length_m=max(0.001, math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))),
        )
        self.document.links.append(link)
        self.modified()
        self.select_element("link", identifier)

    @Slot(str, str)
    def select_element(self, kind, identifier):
        self.selected = (kind, identifier)
        self.editor.selection = self.selected
        self.show_inspector()
        self.editor.update()

    def show_inspector(self):
        while self.inspector_layout.count():
            item = self.inspector_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.fields = {}
        if not self.selected:
            label = QLabel(
                "选择节点或设备编辑参数。\n\n管道可任意连接，不限于示例拓扑。\n\nCAD 三维导入后，请核对 Z 坐标与水力高程。"
            )
            label.setWordWrap(True)
            self.inspector_layout.addWidget(label)
            self.inspector_layout.addStretch()
            return
        kind, identifier = self.selected
        items = self.document.nodes if kind == "node" else self.document.links
        item = next((x for x in items if x.id == identifier), None)
        if item is None:
            self.selected = None
            self.show_inspector()
            return
        title = QLabel(
            f"{identifier} · " + (NODE_NAMES if kind == "node" else LINK_NAMES)[item.kind]
        )
        title.setObjectName("brand")
        self.inspector_layout.addWidget(title)
        form = QFormLayout()
        form.setVerticalSpacing(9)
        form.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        box = QWidget()
        box.setLayout(form)
        self.inspector_layout.addWidget(box)
        identifier_box = QLineEdit(item.id)
        self.fields["id"] = identifier_box
        form.addRow("编号", identifier_box)
        kinds = QComboBox()
        for value, text in (NODE_NAMES if kind == "node" else LINK_NAMES).items():
            kinds.addItem(text, value)
        kinds.setCurrentIndex(kinds.findData(item.kind))
        self.fields["kind"] = kinds
        form.addRow("类型", kinds)
        if kind == "node":
            keys = ["x", "y", "z", "elevation_m"] + (
                {
                    "junction": ["demand_m3h"],
                    "reservoir": ["source_head_m"],
                    "tank": ["tank_level_m", "tank_min_m", "tank_max_m", "tank_diameter_m"],
                }[item.kind]
            )
        else:
            for key in ("start", "end"):
                w = QComboBox()
                w.addItems([n.id for n in self.document.nodes])
                w.setCurrentText(getattr(item, key))
                self.fields[key] = w
                form.addRow("起点" if key == "start" else "终点", w)
            keys = {
                "pipe": ["length_m", "diameter_m", "roughness_m", "minor_loss"],
                "valve": ["diameter_m", "opening"],
                "pump": [
                    "speed_ratio",
                    "nominal_rpm",
                    "motor_efficiency",
                    "drive_efficiency",
                    "motor_rated_kw",
                ],
            }[item.kind]
        for key in keys:
            w = numeric(getattr(item, key))
            self.fields[key] = w
            form.addRow(LABELS[key], w)
        if kind == "link":
            for key, label in (
                [("enabled", "启用设备"), ("check_valve", "管道单向止回")]
                if item.kind == "pipe"
                else [("enabled", "启用设备")]
            ):
                w = QCheckBox()
                w.setChecked(getattr(item, key))
                self.fields[key] = w
                form.addRow(label, w)
            source = QComboBox()
            source.addItems(["hypothetical", "manufacturer", "measured", "cad_geometry"])
            source.setCurrentText(item.source)
            self.fields["source"] = source
            form.addRow("参数来源声明", source)
            if item.kind == "pump":
                curve = QPlainTextEdit(
                    json.dumps([p.model_dump() for p in item.curve], ensure_ascii=False, indent=1)
                )
                curve.setMinimumHeight(170)
                curve.setMaximumHeight(240)
                self.fields["curve"] = curve
                label = QLabel("泵参考曲线 JSON\nflow_m3h、head_m、shaft_kw、npsh3_m")
                label.setWordWrap(True)
                self.inspector_layout.addWidget(label)
                self.inspector_layout.addWidget(curve)
        note = QLineEdit(item.note)
        self.fields["note"] = note
        form.addRow("备注", note)
        self.inspector_layout.addWidget(button("应用参数", self.apply_properties))
        self.inspector_layout.addWidget(QLabel("修改类型后点击应用，展开对应参数。"))
        self.inspector_layout.addStretch()

    def apply_properties(self):
        if not self.selected:
            return
        try:
            kind, identifier = self.selected
            items = self.document.nodes if kind == "node" else self.document.links
            item = next(x for x in items if x.id == identifier)
            value = item.model_dump()
            for key, w in self.fields.items():
                if isinstance(w, QDoubleSpinBox):
                    value[key] = w.value()
                elif isinstance(w, QCheckBox):
                    value[key] = w.isChecked()
                elif isinstance(w, QComboBox):
                    value[key] = w.currentData() if key == "kind" else w.currentText()
                elif isinstance(w, QPlainTextEdit):
                    value[key] = json.loads(w.toPlainText())
                else:
                    value[key] = w.text()
            parsed = (DesignNode if kind == "node" else DesignLink).model_validate(value)
            if any(x.id == parsed.id and x.id != identifier for x in items):
                raise ValueError("编号已存在")
            self.checkpoint()
            items[items.index(item)] = parsed
            if parsed.id != identifier:
                if kind == "node":
                    for l in self.document.links:
                        if l.start == identifier:
                            l.start = parsed.id
                        if l.end == identifier:
                            l.end = parsed.id
                else:
                    for e in self.document.settings.events:
                        if e.target == identifier:
                            e.target = parsed.id
            self.select_element(kind, parsed.id)
            self.modified()
        except Exception as exc:
            self.show_error(str(exc))

    def delete_selection(self):
        if not self.selected:
            return
        kind, identifier = self.selected
        self.checkpoint()
        removed = {identifier}
        if kind == "node":
            self.document.nodes = [n for n in self.document.nodes if n.id != identifier]
            removed |= {l.id for l in self.document.links if identifier in (l.start, l.end)}
            self.document.links = [
                l for l in self.document.links if identifier not in (l.start, l.end)
            ]
        else:
            self.document.links = [l for l in self.document.links if l.id != identifier]
        self.document.settings.events = [
            e for e in self.document.settings.events if e.target not in removed
        ]
        self.selected = None
        self.editor.selection = None
        self.modified()
        self.show_inspector()

    def map_elevation(self):
        self.checkpoint()
        for n in self.document.nodes:
            n.elevation_m = n.z
        self.modified()
        self.show_inspector()
        self.log.appendPlainText("已将所有节点 Z 映射为水力高程；水源总水头仍需单独确认。")

    def split_selected_pipe(self):
        if not self.selected or self.selected[0] != "link":
            self.show_error("请先选择一条管道")
            return
        link = next((l for l in self.document.links if l.id == self.selected[1]), None)
        if link is None or link.kind != "pipe":
            self.show_error("仅可分割普通管道，泵与阀门请通过端点重新连接")
            return
        try:
            doc = self.document.model_copy(deep=True)
            lookup = {n.id: n for n in doc.nodes}
            a, b = lookup[link.start], lookup[link.end]
            points = link.geometry or [(a.x, a.y, a.z), (b.x, b.y, b.z)]
            points = [(a.x, a.y, a.z), *points[1:-1], (b.x, b.y, b.z)]
            lengths = [math.dist(x, y) for x, y in zip(points, points[1:])]
            total = sum(lengths)
            if total <= 1e-9:
                raise ValueError("端点几何重合，无法定位分割点")
            position = total / 2
            accum = 0
            for i, length in enumerate(lengths):
                if accum + length >= position:
                    ratio = (position - accum) / length
                    xyz = tuple(
                        points[i][k] + ratio * (points[i + 1][k] - points[i][k]) for k in range(3)
                    )
                    break
                accum += length
            identifier = self.next_id("J", doc.nodes)
            node = DesignNode(
                id=identifier,
                x=xyz[0],
                y=xyz[1],
                z=xyz[2],
                elevation_m=(a.elevation_m + b.elevation_m) / 2,
                demand_m3h=0,
            )
            doc.nodes.append(node)
            first = link.model_copy(deep=True)
            second = link.model_copy(deep=True)
            first.id = self.next_id("L", doc.links)
            doc.links.append(first)
            second.id = self.next_id("L", doc.links)
            doc.links.append(second)
            first.end = identifier
            second.start = identifier
            first.length_m = link.length_m / 2
            second.length_m = link.length_m / 2
            first.minor_loss = link.minor_loss / 2
            second.minor_loss = link.minor_loss / 2
            first.geometry = [*points[: i + 1], xyz]
            second.geometry = [xyz, *points[i + 1 :]]
            doc.links = [l for l in doc.links if l.id != link.id]
            events = []
            for event in doc.settings.events:
                if event.target == link.id:
                    events.extend(
                        [
                            event.model_copy(update={"target": first.id}),
                            event.model_copy(update={"target": second.id}),
                        ]
                    )
                else:
                    events.append(event)
            doc.settings.events = events
            doc = NetworkDocument.model_validate(doc.model_dump())
            self.checkpoint()
            self.set_document(doc, True, self.file_path)
            self.select_element("node", identifier)
            self.log.appendPlainText(
                f"已将 {link.id} 分为 {first.id} / {second.id}，节点 {identifier} 可接入新支路。"
            )
        except Exception as exc:
            self.show_error(str(exc))

    def validate_document(self):
        try:
            doc = NetworkDocument.model_validate(self.document.model_dump())
            errors = validate_topology(doc)
            self.log.appendPlainText(
                "\n".join(errors) if errors else "结构校验通过。几何可连通不等于工况一定可行。"
            )
        except Exception as exc:
            self.show_error(str(exc))

    def edit_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("仿真设置与控制事件")
        v = QVBoxLayout(dialog)
        form = QFormLayout()
        fields = {}
        for key, value in self.document.settings.model_dump().items():
            if key == "events":
                continue
            w = numeric(value)
            fields[key] = w
            form.addRow(LABELS[key], w)
        v.addLayout(form)
        v.addWidget(QLabel("控制事件 JSON：hour、target、kind（enabled/speed/opening）、value"))
        events = QPlainTextEdit(
            json.dumps(
                [e.model_dump() for e in self.document.settings.events],
                ensure_ascii=False,
                indent=2,
            )
        )
        events.setMinimumHeight(130)
        v.addWidget(events)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        v.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                values = {
                    k: (round(w.value()) if k == "step_seconds" else w.value())
                    for k, w in fields.items()
                }
                values["events"] = json.loads(events.toPlainText())
                settings = DesignSettings.model_validate(values)
                self.checkpoint()
                self.document.settings = settings
                self.modified()
            except Exception as exc:
                self.show_error(str(exc))

    def submit_simulation(self):
        self.submit("design_simulation")

    def submit_optimization(self):
        self.submit("design_optimization")

    def submit(self, kind):
        try:
            doc = NetworkDocument.model_validate(self.document.model_dump())
            errors = validate_topology(doc)
            if errors:
                raise ValueError("\n".join(errors))
            payload = doc.model_dump(mode="json")
            controller = self.controller
            self.work(
                lambda: controller.submit(kind, payload), lambda value: self.task_submitted(value)
            )
        except Exception as exc:
            self.show_error(str(exc))

    def task_submitted(self, value):
        self.log.appendPlainText("任务已提交：" + value["id"])
        self.tabs.setCurrentIndex(2)
        self.refresh()

    def submit_batch(self):
        try:
            values = [
                float(x)
                for x in self.batch_values.text().replace("，", ",").split(",")
                if x.strip()
            ]
            if not 1 <= len(values) <= 12 or any(
                not math.isfinite(x) or not 0.2 <= x <= 1.2 for x in values
            ):
                raise ValueError("每批 1–12 个转速比，范围 0.2–1.2")
            if self.document.settings.events:
                raise ValueError("批量转速扫描前请移除控制事件")
            if not any(l.kind == "pump" for l in self.document.links):
                raise ValueError("当前管网没有泵可扫描")
            errors = validate_topology(self.document)
            if errors:
                raise ValueError("\n".join(errors))
            documents = []
            for speed in values:
                doc = self.document.model_copy(deep=True)
                doc.name = f"{self.document.name} · {speed:.2f} 倍速"[:100]
                for l in doc.links:
                    if l.kind == "pump":
                        l.speed_ratio = speed
                documents.append(doc.model_dump(mode="json"))
            controller = self.controller
            self.work(
                lambda: controller.batch(documents),
                lambda jobs: (
                    self.log.appendPlainText(f"已提交 {len(jobs)} 项批量任务"),
                    self.refresh(),
                ),
            )
        except Exception as exc:
            self.show_error(str(exc))

    def refresh(self):
        if self.polling or self.closing:
            return
        self.polling = True
        controller = self.controller
        self.work(
            lambda: (controller.snapshot(), controller.runs()), self.refreshed, self.refresh_failed
        )

    def refresh_failed(self, message):
        self.polling = False
        self.connection_label.setText("连接失败：" + message[:80])
        self.log.appendPlainText("连接：" + message)

    def refreshed(self, value):
        self.polling = False
        snapshot, runs = value
        self.snapshot = snapshot
        c = snapshot["counts"]
        self.queue_summary.setText(
            f"等待 {c.get('queued', 0)} · 执行 {c.get('running', 0)} · 完成 {c.get('succeeded', 0)} · 失败 {c.get('failed', 0)}"
        )
        self.pause_button.setText("继续队列" if snapshot["paused"] else "暂停队列")
        current = self.selected_job_id()
        self.jobs_table.setRowCount(len(snapshot["jobs"]))
        for i, job in enumerate(snapshot["jobs"]):
            for j, text in enumerate(
                [
                    job["name"],
                    KIND_NAMES.get(job["kind"], job["kind"]),
                    STATE_NAMES[job["state"]],
                    job["error"] or job["message"],
                    job["created_at"][:19].replace("T", " ") + " UTC",
                ]
            ):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, job["id"])
                self.jobs_table.setItem(i, j, item)
            if job["id"] == current:
                self.jobs_table.selectRow(i)
        self.jobs_table.resizeColumnsToContents()
        self.jobs_table.setColumnWidth(0, 250)
        self.jobs_table.setColumnWidth(3, 350)
        selected = self.history.currentData()
        self.history.blockSignals(True)
        self.history.clear()
        for r in runs:
            self.history.addItem(r["name"] + " · " + r["created_at"][:16], r["id"])
        if selected:
            self.history.setCurrentIndex(self.history.findData(selected))
        self.history.blockSignals(False)

    def selected_job_id(self):
        rows = self.jobs_table.selectionModel().selectedRows()
        return (
            self.jobs_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else None
        )

    def pause_queue(self):
        controller = self.controller
        paused = not self.snapshot["paused"]
        self.work(lambda: controller.pause(paused), lambda _: self.refresh())

    def cancel_selected_job(self):
        identifier = self.selected_job_id()
        if identifier:
            controller = self.controller
            self.work(lambda: controller.cancel(identifier), lambda _: self.refresh())

    def open_job_result(self, *args):
        identifier = self.selected_job_id()
        job = next((j for j in self.snapshot["jobs"] if j["id"] == identifier), None)
        if not job:
            return
        result = job.get("result") or {}
        if result.get("design_calibration_id"):
            controller = self.controller
            self.work(
                lambda: controller.calibration(result["design_calibration_id"]),
                self.display_calibration,
            )
        elif result.get("design_run_id"):
            self.load_result_id(result["design_run_id"])
        elif result.get("design_optimization_id"):
            controller = self.controller
            self.work(
                lambda: controller.optimization(result["design_optimization_id"]),
                lambda value: (
                    self.details.setPlainText(json.dumps(value, ensure_ascii=False, indent=2)),
                    self.tabs.setCurrentIndex(3),
                ),
            )
        else:
            self.log.appendPlainText(job.get("error") or "任务尚无可读取的结果。")

    def load_selected_history(self):
        if self.history.currentData():
            self.load_result_id(self.history.currentData())

    def compare_selected_history(self):
        if not self.current_result or not self.history.currentData():
            self.show_error("请先加载一个结果并在历史列表中选择另一个结果")
            return
        controller = self.controller
        identifier = self.history.currentData()
        self.work(lambda: controller.result(identifier), self.show_comparison)

    def show_comparison(self, other):
        current = self.current_result
        a = current["document"]
        b = other["document"]
        keys = [
            "duration_hours",
            "step_seconds",
            "demand_multiplier",
            "temperature_c",
            "required_pressure_m",
        ]

        def boundaries(doc):
            return [
                (
                    n["id"],
                    n["kind"],
                    n["elevation_m"],
                    n["demand_m3h"],
                    n["source_head_m"],
                    n["tank_level_m"],
                )
                for n in doc["nodes"]
            ]

        comparable = (
            all(a["settings"][k] == b["settings"][k] for k in keys)
            and boundaries(a) == boundaries(b)
            and abs(current["summary"]["delivered_m3"] - other["summary"]["delivered_m3"])
            < max(0.01, current["summary"]["delivered_m3"] * 0.01)
        )
        for n in a["nodes"]:
            if n["kind"] == "tank":
                other_node = other["points"][-1]["nodes"].get(n["id"], {})
                comparable = (
                    comparable
                    and abs(
                        current["points"][-1]["nodes"][n["id"]]["tank_level_m"]
                        - (other_node.get("tank_level_m") or 0)
                    )
                    < 0.02
                )
        dialog = QDialog(self)
        dialog.setWindowTitle("运行方案比较")
        dialog.resize(690, 360)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(4, 3)
        table.setHorizontalHeaderLabels(["指标", a["name"], b["name"]])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, (key, label) in enumerate(
            [
                ("energy_kwh", "电能 / kWh"),
                ("cost_yuan", "电费 / 元"),
                ("delivered_m3", "实际供水 / m³"),
                ("unmet_m3", "未满足需求 / m³"),
            ]
        ):
            for column, value in enumerate(
                [label, f"{current['summary'][key]:.4f}", f"{other['summary'][key]:.4f}"]
            ):
                table.setItem(row, column, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        if comparable and current["summary"]["energy_kwh"] > 0:
            message = f"后者相对当前结果的电能差异：{(other['summary']['energy_kwh'] / current['summary']['energy_kwh'] - 1) * 100:.2f}%（负值表示减少）"
        else:
            message = "需求、边界、供水量或期末储水不同，仅展示原始指标，不给出节能率。"
        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def load_result_id(self, identifier):
        controller = self.controller
        self.work(lambda: controller.result(identifier), self.display_result)

    def display_result(self, result):
        NetworkDocument.model_validate(result["document"])
        self.play_timer.stop()
        self.play_button.setText("播放")
        self.current_result = result
        self.current_run_id = result.get("id")
        self.result_canvas.set_document(NetworkDocument.model_validate(result["document"]))
        self.timeline.setRange(0, len(result["points"]) - 1)
        self.timeline.setValue(0)
        self.trend.points = result["points"]
        self.show_frame(0)
        self.trend.network = self.result_canvas.document
        self.trend.pump_id = next(
            (l.id for l in self.trend.network.links if l.kind == "pump"), None
        )
        self.trend.update()
        s = result["summary"]
        self.result_summary.setText(
            f"{result['document']['name']}   |   期间总能耗 {s['energy_kwh']:.3f} kWh   |   实际供水 {s['delivered_m3']:.3f} m³   |   未满足需求 {s['unmet_m3']:.3f} m³   |   {result['meta']['solver']} 已收敛"
        )
        for warning in result["warnings"]:
            self.log.appendPlainText("运行提示：" + warning)
        self.tabs.setCurrentIndex(1)

    def show_frame(self, index):
        if not self.current_result:
            return
        point = self.current_result["points"][index]
        self.result_canvas.point = point
        self.result_canvas.update()
        self.time_label.setText(f"{point['hour']:.2f} h")
        self.trend.cursor = index
        self.trend.update()

    def toggle_play(self):
        if not self.current_result:
            return
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play_button.setText("播放")
        else:
            if self.timeline.value() == self.timeline.maximum():
                self.timeline.setValue(0)
            self.play_timer.start(350)
            self.play_button.setText("暂停")

    def advance_frame(self):
        if self.timeline.value() >= self.timeline.maximum():
            self.play_timer.stop()
            self.play_button.setText("播放")
        else:
            self.timeline.setValue(self.timeline.value() + 1)

    def change_metric(self):
        self.trend.key = self.trend_metric.currentData()
        self.trend.label = self.trend_metric.currentText()
        self.trend.update()

    def change_result_color(self):
        self.result_canvas.color_metric = self.color_mode.currentData()
        self.result_canvas.update()

    def result_selection(self, kind, identifier):
        if not self.current_result:
            return
        if kind == "link" and any(
            l.id == identifier and l.kind == "pump" for l in self.result_canvas.document.links
        ):
            self.trend.pump_id = identifier
            self.trend_metric.setCurrentIndex(self.trend_metric.findData("pump_curve"))
            self.trend.update()
        point = self.current_result["points"][self.timeline.value()]
        values = point["nodes" if kind == "node" else "links"].get(identifier, {})
        self.statusBar().showMessage(
            identifier + " · " + json.dumps(values, ensure_ascii=False), 15000
        )

    def use_result_document(self):
        if self.current_result and self.confirm_discard():
            self.checkpoint()
            self.set_document(NetworkDocument.model_validate(self.current_result["document"]))
            self.tabs.setCurrentIndex(0)

    def import_cad(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 CAD 中心线", "", "DXF 文件 (*.dxf);;所有文件 (*)"
        )
        if not path:
            return
        if Path(path).suffix.lower() != ".dxf":
            self.show_error("当前直接支持 DXF；DWG 请先另存为 DXF。")
            return
        from backend.cad import inspect_dxf

        self.work(lambda: inspect_dxf(path), lambda info: self.cad_options(path, info))

    def cad_options(self, path, info):
        dialog = QDialog(self)
        dialog.setWindowTitle("CAD 导入：单位、图层与连接规则")
        dialog.resize(570, 620)
        v = QVBoxLayout(dialog)
        label = QLabel(info["notice"])
        label.setWordWrap(True)
        v.addWidget(label)
        form = QFormLayout()
        scale = numeric(info["suggested_scale"] or 0.001)
        scale.setDecimals(9)
        scale.setRange(1e-9, 1e6)
        form.addRow("每个 CAD 单位对应米数", scale)
        unitnote = QLabel(
            "米=1；毫米=0.001；厘米=0.01；英尺=0.3048。"
            + (
                "当前文件未声明可识别单位，必须核对。"
                if info["suggested_scale"] is None
                else f"已按 INSUNITS={info['units_code']} 预填。"
            )
        )
        unitnote.setWordWrap(True)
        v.addWidget(unitnote)
        tolerance = numeric(0.01)
        tolerance.setRange(0.00001, 10)
        form.addRow("三维端点连接容差 / m", tolerance)
        curve = numeric(0.01)
        curve.setRange(0.00001, 10)
        form.addRow("弧/样条离散容差 / m", curve)
        diameter = numeric(0.05)
        diameter.setRange(0.001, 10)
        form.addRow("待确认默认内径 / m", diameter)
        v.addLayout(form)
        layers = QListWidget()
        for name in info["layers"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            layers.addItem(item)
        v.addWidget(QLabel("仅选择管道中心线图层："))
        v.addWidget(layers)
        z = QCheckBox("将 CAD 的 Z 坐标作为初始水力高程（导入后仍需确认）")
        z.setChecked(True)
        v.addWidget(z)
        tees = QCheckBox("端点落在同高程线段内部时分割成 T 接；内部交叉不自动连接")
        tees.setChecked(True)
        v.addWidget(tees)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        v.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        from backend.cad import import_dxf

        selected = [
            layers.item(i).text()
            for i in range(layers.count())
            if layers.item(i).checkState() == Qt.CheckState.Checked
        ]
        values = {
            "scale": scale.value(),
            "tolerance_m": tolerance.value(),
            "curve_tolerance_m": curve.value(),
            "layers": selected,
            "z_to_elevation": z.isChecked(),
            "connect_endpoint_to_segment": tees.isChecked(),
            "diameter_m": diameter.value(),
        }

        def imported(doc):
            self.checkpoint()
            self.set_document(doc)
            self.view_mode.setCurrentIndex(1)
            self.log.appendPlainText(
                f"已导入 {len(doc.nodes)} 节点、{len(doc.links)} 管段。请设置水源、需水节点和设备。"
            )
            self.show_cad_report()
            self.tabs.setCurrentIndex(0)

        self.work(lambda: import_dxf(path, **values), imported)

    def show_cad_report(self):
        self.details.setPlainText(
            json.dumps(
                self.document.cad_metadata or {"notice": "当前工程未从 CAD 导入"},
                ensure_ascii=False,
                indent=2,
            )
        )
        self.tabs.setCurrentIndex(3)

    def inspect_json(self):
        self.details.setPlainText(self.document.model_dump_json(indent=2))

    def edit_json(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑完整工程 JSON")
        dialog.resize(850, 700)
        v = QVBoxLayout(dialog)
        text = QPlainTextEdit(self.document.model_dump_json(indent=2))
        v.addWidget(text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        v.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                doc = NetworkDocument.model_validate_json(text.toPlainText())
                self.checkpoint()
                self.set_document(doc, True, self.file_path)
            except Exception as exc:
                self.show_error(str(exc))

    def export_inp(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 EPANET 网络", self.document.name + ".inp", "EPANET (*.inp)"
        )
        if path:
            doc = self.document.model_copy(deep=True)
            self.work(
                lambda: export_design_inp(doc),
                lambda content: Path(path).write_text(content, encoding="utf-8"),
            )

    def export_result(self):
        if not self.current_result:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出完整结果", "result.json", "JSON (*.json)")
        if path:
            Path(path).write_text(
                json.dumps(self.current_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def import_result(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入完整结果", "", "JSON (*.json)")
        if path:
            try:
                self.display_result(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            except Exception as exc:
                self.show_error(str(exc))

    def export_csv(self):
        if not self.current_result:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出节点与设备时间序列", "timeseries.csv", "CSV (*.csv)"
        )
        if not path:
            return
        with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["hour", "type", "id", "pressure_m", "flow_m3h", "power_kw", "tank_level_m"]
            )
            for p in self.current_result["points"]:
                for identifier, n in p["nodes"].items():
                    writer.writerow(
                        [
                            p["hour"],
                            "node",
                            identifier,
                            n["pressure_m"],
                            n["demand_m3h"],
                            "",
                            n["tank_level_m"],
                        ]
                    )
                for identifier, l in p["links"].items():
                    writer.writerow(
                        [p["hour"], "link", identifier, "", l["flow_m3h"], l["power_kw"], ""]
                    )

    def closeEvent(self, event):
        if not self.confirm_discard():
            event.ignore()
            return
        if any(j["state"] in ("running", "cancelling") for j in self.snapshot["jobs"]):
            if (
                QMessageBox.question(
                    self, "本机任务仍在执行", "关闭窗口会请求停止本机计算，排队任务保留。是否关闭？"
                )
                != QMessageBox.StandardButton.Yes
            ):
                event.ignore()
                return
        self.closing = True
        self.poll_timer.stop()
        self.play_timer.stop()
        self.controller.close()
        self.pool.waitForDone(32000)
        event.accept()
