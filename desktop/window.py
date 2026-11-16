import csv
import json
import math
import uuid
from pathlib import Path

from datetime import datetime

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    QSize,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from backend.models import DesignLink, DesignNode, DesignSettings, NetworkDocument, default_document
from backend.physics import export_design_inp, validate_topology
from . import theme
from .canvas import NetworkCanvas, TrendWidget
from .controller import Controller
from .scrolling import fit_to_screen, scrollable
from .widgets import CurveEditor, ElementBrowser, FindingList, KpiCard

STATE_NAMES = {
    "queued": "等待",
    "running": "执行中",
    "cancelling": "等待取消",
    "succeeded": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}
KIND_NAMES = {
    "physics_cfd": "三维流场",
    "physics_scan": "汽蚀扫描",
    "physics_mesh": "网格校核",
    "physics_timestep": "步长校核",
    "physics_transient": "管网瞬变",
    "physics_coupled": "双向耦合",
    "physics_validation": "物理实测对比",
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
    "diameter_m": "管道/阀门内径 / mm",
    "roughness_m": "绝对粗糙度 / mm",
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


# key: (decimals, step, minimum, maximum, display scale)
FIELD_SPECS = {
    "x": (3, 1, -1e7, 1e7, 1),
    "y": (3, 1, -1e7, 1e7, 1),
    "z": (3, 0.5, -1e5, 1e5, 1),
    "elevation_m": (3, 0.5, -1e5, 1e5, 1),
    "demand_m3h": (3, 0.5, 0, 1e6, 1),
    "source_head_m": (3, 0.5, -1e5, 1e5, 1),
    "tank_level_m": (3, 0.1, 0, 1e4, 1),
    "tank_min_m": (3, 0.1, 0, 1e4, 1),
    "tank_max_m": (3, 0.1, 0.001, 1e4, 1),
    "tank_diameter_m": (3, 0.1, 0.001, 1e4, 1),
    "length_m": (3, 1, 0.001, 1e7, 1),
    "diameter_m": (1, 5, 0.1, 100000, 1000),
    "roughness_m": (4, 0.01, 0, 1000, 1000),
    "minor_loss": (3, 0.1, 0, 1e7, 1),
    "opening": (3, 0.05, 0, 1, 1),
    "speed_ratio": (3, 0.05, 0.2, 1.2, 1),
    "nominal_rpm": (0, 50, 1, 1e5, 1),
    "motor_efficiency": (3, 0.01, 0.01, 1, 1),
    "drive_efficiency": (3, 0.01, 0.01, 1, 1),
    "motor_rated_kw": (3, 0.1, 0.001, 1e6, 1),
    "duration_hours": (3, 0.25, 0.05, 168, 1),
    "step_seconds": (0, 60, 10, 3600, 1),
    "demand_multiplier": (3, 0.05, 0.01, 10, 1),
    "required_pressure_m": (2, 1, 0.01, 1000, 1),
    "temperature_c": (1, 1, 5, 60, 1),
    "atmospheric_kpa": (3, 0.1, 65, 110, 1),
    "electricity_price": (3, 0.05, 0, 100, 1),
}


def numeric(value, key=None):
    w = QDoubleSpinBox()
    decimals, step, low, high, scale = FIELD_SPECS.get(key, (6, 0.1, -1e8, 1e8, 1))
    w.setDecimals(decimals)
    w.setRange(low, high)
    w.setProperty("scale", scale)
    w.setValue(float(value) * scale)
    w.setSingleStep(step)
    w.setKeyboardTracking(False)
    w.setMinimumHeight(28)
    w.setMinimumWidth(110)
    w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    return w


def spin_value(w):
    """Model value of a spin box created by :func:`numeric` (undoes display scaling)."""
    return w.value() / (w.property("scale") or 1)


def local_time(text):
    """ISO timestamp (UTC) → local "YYYY-MM-DD HH:MM"."""
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return (text or "")[:16].replace("T", " ")


def duration_text(started, finished, state):
    try:
        start = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return ""
    try:
        end = datetime.fromisoformat(finished) if finished else datetime.now(start.tzinfo)
    except (TypeError, ValueError):
        return ""
    seconds = max(0.0, (end - start).total_seconds())
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def friendly_error(exc):
    """Readable message for pydantic validation errors."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            parts = []
            for e in errors():
                where = ".".join(str(x) for x in e.get("loc", ()))
                label = LABELS.get(where, where)
                message = e.get("msg", "").replace("Value error, ", "")
                parts.append(f"{label}：{message}" if label else message)
            if parts:
                return "；".join(parts)
        except Exception:
            pass
    return str(exc)


def primary(text, callback):
    w = button(text, callback)
    w.setObjectName("primary")
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


TOOLS = [
    ("选择", "select", "V", "选择 / 移动（V）"),
    ("节点", "junction", "J", "放置需水 / 连接节点（J）"),
    ("水源", "reservoir", "R", "放置定水头水源（R）"),
    ("水箱", "tank", "T", "放置水箱（T）"),
    ("管道", "pipe", "L", "依次点击两节点连接管道（L）"),
    ("水泵", "pump", "P", "依次点击吸入、出口节点连接离心泵（P）"),
    ("阀门", "valve", "K", "依次点击两节点连接调节阀（K）"),
]
MAX_RECENT = 8


class MainWindow(QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.setWindowTitle("FLOWSTATE · 二维/三维管网设计与数字孪生")
        self.resize(1440, 900)
        self.document = default_document()
        self.file_path = None
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.selected = None
        self.fields = {}
        self.current_result = None
        self.current_run_id = None
        self.diagnostics = None
        self.result_element = None
        self.design_flags = {}
        self.results = {}
        self.snapshot = {"jobs": [], "counts": {}, "paused": False}
        self.settings = QSettings("Flowstate", "FLOWSTATE")
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.callbacks = {}
        self.workers = {}
        self.polling = False
        self.closing = False
        self.controller = Controller(data_dir)
        self._build()
        self._style()
        fit_to_screen(self, 1440, 900)
        self.setAcceptDrops(True)
        self._refresh_title()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh)
        self.poll_timer.start(1500)
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self.advance_frame)
        self.refresh()

    # ------------------------------------------------------------- actions
    def _action(self, text, callback, shortcut=None, tip=None):
        action = QAction(text, self)
        action.triggered.connect(lambda checked=False: callback())
        if shortcut is not None:
            action.setShortcut(shortcut)
        if tip:
            action.setStatusTip(tip)
            action.setToolTip(tip)
        return action

    def _build_actions(self):
        S = QKeySequence.StandardKey
        a = {}
        a["new"] = self._action("新建", self.new_project, S.New, "新建空白管网（⌘/Ctrl+N）")
        a["open"] = self._action("打开…", self.open_project, S.Open, "打开工程 JSON（⌘/Ctrl+O）")
        a["save"] = self._action("保存", self.save_project, S.Save, "保存工程（⌘/Ctrl+S）")
        a["save_as"] = self._action("另存为…", lambda: self.save_project(True), S.SaveAs)
        a["undo"] = self._action("撤销", self.undo, S.Undo, "撤销（⌘/Ctrl+Z）")
        a["redo"] = self._action("重做", self.redo, S.Redo, "重做")
        a["cad"] = self._action("导入 CAD / DXF…", self.import_cad, None, "导入 DXF 管道中心线")
        a["inp"] = self._action("导出 EPANET INP…", self.export_inp)
        a["split"] = self._action("分割所选管段", self.split_selected_pipe, None,
                                  "在所选管道中点插入节点，便于接出支路")
        a["delete"] = self._action("删除所选", self.delete_selection)
        a["map_z"] = self._action("将 Z 映射为水力高程", self.map_elevation)
        a["settings"] = self._action("仿真设置…", self.edit_settings, None, "时长、步长、电价与控制事件")
        a["validate"] = self._action("校验管网", self.validate_document, None,
                                     "检查拓扑与参数，并在图上标出问题元素")
        a["run"] = self._action("运行仿真", self.submit_simulation, QKeySequence("Ctrl+R"),
                                "校验后加入本机仿真队列（⌘/Ctrl+R）")
        a["optimize"] = self._action("搜索低能耗方案", self.submit_optimization)
        a["focus"] = self._action("成果聚焦", self.open_focus, None, "把结果图和曲线放大到同一屏")
        a["fit"] = self._action("适应视图", lambda: self.active_canvas().fit(), QKeySequence("Ctrl+0"))
        a["zoom_in"] = self._action("放大", lambda: self.active_canvas().zoom_by(1.25),
                                    QKeySequence.StandardKey.ZoomIn)
        a["zoom_out"] = self._action("缩小", lambda: self.active_canvas().zoom_by(0.8),
                                     QKeySequence.StandardKey.ZoomOut)
        a["labels"] = QAction("显示编号与数值标签", self, checkable=True, checked=True)
        a["labels"].toggled.connect(self.toggle_labels)
        a["shortcuts"] = self._action("快捷键", self.show_shortcuts)
        a["about"] = self._action("关于 FLOWSTATE", self.show_about)
        a["quit"] = self._action("退出", self.close, QKeySequence.StandardKey.Quit)
        self.actions_map = a
        return a

    def _build_menus(self, a):
        bar = self.menuBar()
        m = bar.addMenu("文件")
        for key in ("new", "open"):
            m.addAction(a[key])
        self.recent_menu = m.addMenu("最近打开")
        self._refresh_recent_menu()
        m.addSeparator()
        for key in ("save", "save_as"):
            m.addAction(a[key])
        m.addSeparator()
        m.addAction(a["cad"])
        m.addAction(a["inp"])
        m.addSeparator()
        m.addAction(a["quit"])
        m = bar.addMenu("编辑")
        for key in ("undo", "redo", None, "delete", "split", "map_z"):
            m.addSeparator() if key is None else m.addAction(a[key])
        m = bar.addMenu("视图")
        for key in ("fit", "zoom_in", "zoom_out", None, "labels"):
            m.addSeparator() if key is None else m.addAction(a[key])
        m = bar.addMenu("仿真")
        for key in ("settings", "validate", None, "run", "optimize", None, "focus"):
            m.addSeparator() if key is None else m.addAction(a[key])
        m = bar.addMenu("帮助")
        m.addAction(a["shortcuts"])
        m.addAction(a["about"])

    def _build_toolbar(self, a):
        toolbar = QToolBar("项目")
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)
        brand = QLabel("FLOWSTATE")
        brand.setObjectName("brand")
        brand.setToolTip("离心泵与二维 / 三维管网 · 设计 · 仿真 · 诊断")
        toolbar.addWidget(brand)
        toolbar.addSeparator()
        for key in ("new", "open", "save", None, "undo", "redo", None, "cad", "inp", None,
                    "settings", "validate"):
            toolbar.addSeparator() if key is None else toolbar.addAction(a[key])
        toolbar.addSeparator()
        run = QToolButton()
        run.setDefaultAction(a["run"])
        run.setObjectName("primaryTool")
        run.setStyleSheet(
            f"QToolButton{{background:{theme.ACCENT};color:white;border-radius:6px;"
            f"padding:5px 14px;font-weight:600;}}QToolButton:hover{{background:{theme.ACCENT_DARK};}}"
        )
        toolbar.addWidget(run)
        toolbar.addAction(a["focus"])
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.dirty_pill = QLabel("未保存")
        self.dirty_pill.setObjectName("dirtyPill")
        self.dirty_action = toolbar.addWidget(self.dirty_pill)
        self.dirty_action.setVisible(False)
        self.project_pill = QLabel()
        self.project_pill.setObjectName("pill")
        toolbar.addWidget(self.project_pill)

    def _build(self):
        a = self._build_actions()
        self._build_menus(a)
        self._build_toolbar(a)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(8)
        self.main_scroll = scrollable(container, "mainContentScroll")
        self.setCentralWidget(self.main_scroll)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        self.tabs.currentChanged.connect(self.tab_changed)
        self._build_design()
        self._build_results()
        self._build_dispatch()
        self._build_data()
        self._build_observations()
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(96)
        self.log.setMinimumHeight(44)
        self.log.setPlaceholderText("运行日志：任务提交、校验结果与错误提示会显示在这里。")
        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)
        body.addWidget(self.tabs)
        body.addWidget(self.log)
        body.setStretchFactor(0, 1)
        layout.addWidget(body, 1)
        self.cursor_label = QLabel("")
        self.zoom_label = QLabel("")
        self.connection_label = QLabel("● 本机计算 · 数据保存在本机")
        self.connection_label.setToolTip("全部计算与数据存储均在本机完成，不连接远程服务")
        self.connection_label.setStyleSheet(f"color:{theme.ACCENT};")
        self.statusBar().addPermanentWidget(self.cursor_label)
        self.statusBar().addPermanentWidget(self.zoom_label)
        self.statusBar().addPermanentWidget(self.connection_label)
        self.statusBar().showMessage(
            "全部演示参数尚未完成实机验证；CAD 中心线导入后需补充水力边界。"
        )
        self.refresh_browser()

    # --------------------------------------------------------------- design
    def _build_design(self):
        design = QWidget()
        self.design_page = design
        designlayout = QVBoxLayout(design)
        designlayout.setContentsMargins(10, 10, 10, 6)
        designlayout.setSpacing(8)
        tools = QHBoxLayout()
        tools.setSpacing(0)
        # The combo boxes remain the single source of truth (scripts and tests
        # drive them); the segmented buttons are the visible controls.
        self.tool = QComboBox(design)
        for text, value, _, _ in TOOLS:
            self.tool.addItem(text, value)
        self.tool.hide()
        self.tool.currentIndexChanged.connect(self.change_tool)
        self.tool_buttons = QButtonGroup(self)
        self.tool_buttons.setExclusive(True)
        for i, (text, value, key, tip) in enumerate(TOOLS):
            b = QToolButton()
            b.setText(f"{text}")
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setObjectName("toolFirst" if i == 0 else ("toolLast" if i == len(TOOLS) - 1 else "tool"))
            self.tool_buttons.addButton(b, i)
            tools.addWidget(b)
            shortcut = QAction(self)
            shortcut.setShortcut(QKeySequence(key))
            shortcut.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.triggered.connect(lambda checked=False, i=i: self.tool.setCurrentIndex(i))
            design.addAction(shortcut)
        self.tool_buttons.button(0).setChecked(True)
        self.tool_buttons.idClicked.connect(self.tool.setCurrentIndex)
        tools.addSpacing(14)
        self.view_mode = QComboBox(design)
        self.view_mode.addItem("二维 XY 编辑", "2d")
        self.view_mode.addItem("三维旋转视图", "3d")
        self.view_mode.hide()
        self.view_mode.currentIndexChanged.connect(self.change_view_mode)
        self.view_buttons = QButtonGroup(self)
        for i, text in enumerate(["二维 XY", "三维"]):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setObjectName("toolFirst" if i == 0 else "toolLast")
            b.setToolTip("二维 XY 编辑" if i == 0 else "三维旋转视图（只读几何，可选择元素）")
            self.view_buttons.addButton(b, i)
            tools.addWidget(b)
        self.view_buttons.button(0).setChecked(True)
        self.view_buttons.idClicked.connect(self.view_mode.setCurrentIndex)
        tools.addSpacing(14)
        self.snap_box = QCheckBox("网格吸附 1 m")
        self.snap_box.setToolTip("放置与拖动节点时吸附到 1 m 网格")
        self.snap_box.toggled.connect(self.toggle_snap)
        tools.addWidget(self.snap_box)
        tools.addSpacing(8)
        tools.addWidget(button("适应视图", lambda: self.editor.fit()))
        tools.addSpacing(4)
        tools.addWidget(button("删除所选", self.delete_selection))
        tools.addStretch()
        tools.addWidget(button("搜索低能耗方案", self.submit_optimization))
        tools.addSpacing(6)
        tools.addWidget(primary("▶ 加入仿真队列", self.submit_simulation))
        designlayout.addLayout(tools)
        split = QSplitter()
        split.setChildrenCollapsible(True)
        self.browser = ElementBrowser()
        self.browser.picked.connect(self.pick_from_browser)
        split.addWidget(self.browser)
        self.editor = NetworkCanvas(self.document)
        self.editor.changed_mode = lambda: self.tool.setCurrentIndex(0)
        split.addWidget(self.editor)
        self.editor.selected.connect(self.select_element)
        self.editor.deselected.connect(self.clear_selection)
        self.editor.add_requested.connect(self.add_node)
        self.editor.connect_requested.connect(self.add_link)
        self.editor.begin_edit.connect(self.checkpoint)
        self.editor.changed.connect(self.canvas_changed)
        self.editor.delete_requested.connect(self.delete_selection)
        self.editor.cursor_moved.connect(
            lambda x, y: self.cursor_label.setText(f"X {x:.2f} m · Y {y:.2f} m")
        )
        self.editor.zoom_changed.connect(lambda z: self.zoom_label.setText(f"缩放 {z * 100:.0f}%"))
        self.inspector = QWidget()
        self.inspector_layout = QVBoxLayout(self.inspector)
        self.inspector_layout.setContentsMargins(14, 4, 8, 8)
        self.inspector_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        scroll = scrollable(self.inspector, "inspectorScroll")
        self.inspector_scroll = scroll
        scroll.setMinimumWidth(340)
        scroll.setMaximumWidth(460)
        split.addWidget(scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([210, 860, 380])
        self.design_split = split
        designlayout.addWidget(split, 1)
        self.tabs.addTab(design, "管网设计")
        self.show_inspector()

    # -------------------------------------------------------------- results
    def _build_results(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 6)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(QLabel("历史结果"))
        self.history = QComboBox()
        self.history.setMinimumWidth(240)
        row.addWidget(self.history, 1)
        row.addWidget(button("读取", self.load_selected_history))
        row.addWidget(button("与当前结果比较", self.compare_selected_history))
        row.addWidget(button("作为新草稿", self.use_result_document))
        export = QToolButton()
        export.setText("导出 ▾")
        export.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(export)
        menu.addAction("完整结果 JSON…", self.export_result)
        menu.addAction("时间序列 CSV…", self.export_csv)
        menu.addAction("诊断报告 CSV…", self.export_diagnostics)
        menu.addSeparator()
        menu.addAction("结果图 PNG…", lambda: self.export_png(self.result_canvas, "network.png"))
        menu.addAction("趋势图 PNG…", lambda: self.export_png(self.trend, "trend.png"))
        export.setMenu(menu)
        row.addWidget(export)
        v.addLayout(row)
        self.result_summary = QLabel("计算完成后，在“任务队列”中选择任务并点击“查看结果”，或双击任务。")
        self.result_summary.setObjectName("summary")
        self.result_summary.setWordWrap(True)
        v.addWidget(self.result_summary)
        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.kpis = {}
        for key, title, unit in [
            ("energy", "期间能耗", "kWh"),
            ("cost", "电费", "元"),
            ("delivered", "实际供水", "m³"),
            ("unmet", "未满足需水", "m³"),
            ("specific", "单位供水能耗", "kWh/m³"),
            ("efficiency", "泵组综合效率", "%"),
            ("pressure", "最低末端压力", "m"),
            ("health", "运行诊断", ""),
        ]:
            card = KpiCard(title, unit)
            self.kpis[key] = card
            cards.addWidget(card)
        v.addLayout(cards)
        middle = QSplitter(Qt.Orientation.Horizontal)
        middle.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        self.result_canvas = NetworkCanvas(default_document(), editable=False)
        self.result_canvas.set_dimension("3d")
        self.result_canvas.color_metric = "combined"
        self.result_canvas.selected.connect(self.result_selection)
        self.result_canvas.zoom_changed.connect(
            lambda z: self.zoom_label.setText(f"缩放 {z * 100:.0f}%")
        )
        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.setChildrenCollapsible(False)
        vertical.addWidget(self.result_canvas)
        trend_box = QWidget()
        tv = QVBoxLayout(trend_box)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        controls = QHBoxLayout()
        self.dimension_button = button("切换二维 / 三维", self.toggle_result_dimension)
        controls.addWidget(self.dimension_button)
        self.color_mode = QComboBox()
        self.color_mode.addItem("显示：节点压力 + 管道流速", "combined")
        self.color_mode.addItem("显示：压力（节点与沿管渐变）", "pressure")
        self.color_mode.addItem("显示：管道流速", "velocity")
        self.color_mode.addItem("显示：管道流量", "flow")
        self.color_mode.addItem("显示：单位水头损失", "headloss")
        self.color_mode.setToolTip("选择图上节点、管道的着色参数；颜色范围按整个模拟期间统一")
        self.color_mode.currentIndexChanged.connect(self.change_result_color)
        controls.addWidget(self.color_mode)
        self.flow_animation = QCheckBox("水流动画")
        self.flow_animation.setToolTip("沿流向移动的粒子，移动越快表示流速越大")
        self.flow_animation.setChecked(True)
        self.flow_animation.toggled.connect(lambda v: self.result_canvas.set_animate(v))
        controls.addWidget(self.flow_animation)
        self.play_button = primary("播放", self.toggle_play)
        self.play_button.setMinimumWidth(70)
        controls.addWidget(self.play_button)
        self.play_speed = QComboBox()
        for text, ms in [("0.5×", 700), ("1×", 350), ("2×", 175), ("4×", 90)]:
            self.play_speed.addItem(text, ms)
        self.play_speed.setCurrentIndex(1)
        self.play_speed.currentIndexChanged.connect(
            lambda: self.play_timer.setInterval(self.play_speed.currentData())
        )
        controls.addWidget(self.play_speed)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.valueChanged.connect(self.show_frame)
        controls.addWidget(self.timeline, 1)
        self.time_label = QLabel("0.00 h")
        self.time_label.setMinimumWidth(64)
        controls.addWidget(self.time_label)
        controls.addSpacing(10)
        self.trend_metric = QComboBox()
        self.trend_metric.addItem("电功率 / kW", "power_kw")
        self.trend_metric.addItem("实际供水 / m³/h", "delivered_m3h")
        self.trend_metric.addItem("未满足需求 / m³/h", "unmet_m3h")
        self.trend_metric.addItem("最低末端压力 / m", "min_pressure_m")
        self.trend_metric.addItem("所选泵 Q–H 性能曲线", "pump_curve")
        self.trend_metric.addItem("所选元素时序（压力 / 流量）", "element")
        self.trend_metric.addItem("沿程水力剖面（水源 → 所选节点）", "profile")
        self.trend_metric.currentIndexChanged.connect(self.change_metric)
        controls.addWidget(self.trend_metric)
        tv.addLayout(controls)
        self.trend = TrendWidget()
        tv.addWidget(self.trend, 1)
        vertical.addWidget(trend_box)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        lv.addWidget(vertical, 1)
        middle.addWidget(left)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(6, 0, 0, 0)
        sv.setSpacing(6)
        self.element_title = QLabel("所选元素")
        self.element_title.setObjectName("sectionTitle")
        sv.addWidget(self.element_title)
        self.element_table = QTableWidget(0, 2)
        self.element_table.setHorizontalHeaderLabels(["量", "当前时刻"])
        self.element_table.verticalHeader().setVisible(False)
        self.element_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.element_table.horizontalHeader().setStretchLastSection(True)
        self.element_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.element_table.setMinimumHeight(120)
        sv.addWidget(self.element_table, 2)
        diag_title = QLabel("运行诊断")
        diag_title.setObjectName("sectionTitle")
        sv.addWidget(diag_title)
        self.findings = FindingList()
        self.findings.picked.connect(self.pick_finding)
        sv.addWidget(self.findings, 3)
        note = QLabel("经验阈值：流速 ≤ 2.5 m/s；泵 60–120% 最高效率点流量；NPSHa ≥ max(1.3·NPSH3, NPSH3+0.5 m)。")
        note.setObjectName("hint")
        note.setWordWrap(True)
        sv.addWidget(note)
        side.setMinimumWidth(300)
        middle.addWidget(side)
        middle.setStretchFactor(0, 3)
        middle.setStretchFactor(1, 1)
        middle.setSizes([980, 380])
        v.addWidget(middle, 1)
        self.tabs.addTab(page, "结果与诊断")

    # ------------------------------------------------------------- dispatch
    def _build_dispatch(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 6)
        row = QHBoxLayout()
        self.queue_summary = QLabel("计算引擎就绪")
        self.queue_summary.setObjectName("sectionTitle")
        row.addWidget(self.queue_summary, 1)
        self.pause_button = button("暂停队列", self.pause_queue)
        row.addWidget(self.pause_button)
        row.addWidget(button("刷新", self.refresh))
        row.addWidget(button("取消所选任务", self.cancel_selected_job))
        row.addWidget(primary("查看任务结果", self.open_job_result))
        v.addLayout(row)
        self.jobs_table = QTableWidget(0, 7)
        self.jobs_table.setHorizontalHeaderLabels(
            ["任务名称", "类型", "状态", "进度", "执行阶段", "提交时间", "用时"]
        )
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.setAlternatingRowColors(True)
        self.jobs_table.verticalHeader().setVisible(False)
        header = self.jobs_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column, width in [(0, 240), (1, 110), (2, 80), (3, 110), (5, 150), (6, 70)]:
            self.jobs_table.setColumnWidth(column, width)
        self.jobs_table.itemDoubleClicked.connect(self.open_job_result)
        v.addWidget(self.jobs_table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("批量转速比："))
        self.batch_values = QLineEdit("0.85, 0.95, 1.05")
        self.batch_values.setToolTip("逗号分隔，1–12 个值，范围 0.2–1.2；所有泵同时设定")
        row.addWidget(self.batch_values, 1)
        row.addWidget(button("创建批量仿真", self.submit_batch))
        v.addLayout(row)
        label = QLabel("暂停只阻止启动后续任务；原生水力调用不能即时中断，取消将在返回后生效。双击已完成任务可直接查看结果。")
        label.setObjectName("hint")
        label.setWordWrap(True)
        v.addWidget(label)
        self.tabs.addTab(page, "任务队列")

    def _build_data(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 6)
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
        self.details.setObjectName("code")
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
        v.setContentsMargins(10, 10, 10, 6)
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
        self.observation_text.setObjectName("code")
        self.observation_text.setPlaceholderText(
            "timestamp,pressure_m,flow_m3h,power_kw\n2026-11-03T06:00:00+08:00,17.0,10.5,1.29"
        )
        v.addWidget(self.observation_text, 1)
        self.calibration_output = QPlainTextEdit()
        self.calibration_output.setObjectName("code")
        self.calibration_output.setReadOnly(True)
        v.addWidget(self.calibration_output, 1)
        self.last_calibration = None
        self.tabs.addTab(page, "观测对照与校准")
        from .physics_panel import PhysicsPanel

        self.physics_panel = PhysicsPanel(self)
        self.tabs.addTab(self.physics_panel, "三维物理与瞬变")
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
        self.setStyleSheet(theme.STYLE)

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
        name = self.document.name
        if self.file_path:
            name += f" — {Path(self.file_path).name}"
        self.setWindowTitle(f"FLOWSTATE · {name}" + (" *" if self.dirty else ""))
        if hasattr(self, "dirty_pill"):
            self.dirty_action.setVisible(self.dirty)
            self.project_pill.setText(
                f"{self.document.name} · {len(self.document.nodes)} 节点 / {len(self.document.links)} 连接"
            )

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
        self.refresh_browser()

    def canvas_changed(self):
        self.modified()
        self.show_inspector()

    def set_document(self, doc, dirty=True, path=None):
        self.document = doc
        self.file_path = path
        self.dirty = dirty
        self.selected = None
        self.design_flags = {}
        self.editor.set_document(doc)
        self.show_inspector()
        self._refresh_title()
        self.update_observation_targets()
        self.refresh_browser()

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
            self.add_recent(path)
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
            self.add_recent(path)
            self.log.appendPlainText("已保存：" + str(path))
            self.statusBar().showMessage("已保存：" + str(path), 5000)
        except Exception as exc:
            self.show_error(str(exc))

    def change_tool(self):
        self.editor.mode = self.tool.currentData()
        self.editor.connection_start = None
        b = self.tool_buttons.button(self.tool.currentIndex())
        if b is not None and not b.isChecked():
            b.setChecked(True)
        hints = {
            "select": "选择 / 移动：点击元素编辑参数，拖动节点，拖动空白处平移。",
            "junction": "点击画布放置需水 / 连接节点。Esc 返回选择。",
            "reservoir": "点击画布放置定水头水源。",
            "tank": "点击画布放置水箱。",
            "pipe": "依次点击起点、终点节点连接管道。",
            "pump": "依次点击吸入节点、出口节点连接离心泵。",
            "valve": "依次点击两个连接节点放置调节阀。",
        }
        self.statusBar().showMessage(hints.get(self.editor.mode, ""), 8000)
        self.editor.update()

    def change_view_mode(self):
        index = self.view_mode.currentIndex()
        b = self.view_buttons.button(index)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        self.editor.set_dimension(self.view_mode.currentData())

    def toggle_snap(self, value):
        self.editor.snap = value
        self.editor.update()

    def toggle_labels(self, value):
        for canvas in (self.editor, self.result_canvas):
            canvas.show_labels = value
            canvas.update()

    def active_canvas(self):
        return self.result_canvas if self.tabs.currentIndex() == 1 else self.editor

    def tab_changed(self, index):
        if not hasattr(self, "zoom_label"):
            return
        if index != 0:
            self.cursor_label.setText("")
        canvas = self.active_canvas()
        self.zoom_label.setText(f"缩放 {canvas.zoom * 100:.0f}%")

    def open_focus(self):
        from .focus import open_current_results

        open_current_results(self)

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
        self.browser.select(kind, identifier)
        self.show_inspector()
        self.editor.update()

    def clear_selection(self):
        if self.selected is None:
            return
        self.selected = None
        self.editor.selection = None
        self.browser.select("", "")
        self.show_inspector()

    def pick_from_browser(self, kind, identifier):
        self.select_element(kind, identifier)
        self.editor.setFocus()

    def refresh_browser(self):
        if hasattr(self, "browser"):
            self.browser.set_document(self.document, self.design_flags)

    def _mark_changed(self, widget):
        widget.setProperty("changed", True)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        if getattr(self, "pending_label", None) is not None:
            self.pending_label.show()

    def show_inspector(self):
        while self.inspector_layout.count():
            item = self.inspector_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                item.layout().deleteLater()
        self.fields = {}
        self.pending_label = None
        if not self.selected:
            title = QLabel("参数面板")
            title.setObjectName("sectionTitle")
            self.inspector_layout.addWidget(title)
            label = QLabel(
                "在画布或左侧元素列表中选择节点 / 设备，即可在此编辑参数。\n\n"
                "快捷键：V 选择 · J 节点 · R 水源 · T 水箱 · L 管道 · P 水泵 · K 阀门 · "
                "Delete 删除 · Esc 取消 · F 适应视图\n\n"
                "CAD 三维导入后，请核对 Z 坐标与水力高程。"
            )
            label.setWordWrap(True)
            self.inspector_layout.addWidget(label)
            doc = self.document
            stats = QLabel(
                f"当前工程：{len(doc.nodes)} 节点（"
                f"{sum(n.kind == 'reservoir' for n in doc.nodes)} 水源、"
                f"{sum(n.kind == 'tank' for n in doc.nodes)} 水箱）· {len(doc.links)} 连接（"
                f"{sum(l.kind == 'pump' for l in doc.links)} 泵、"
                f"{sum(l.kind == 'valve' for l in doc.links)} 阀）\n"
                f"总需水 {sum(n.demand_m3h for n in doc.nodes):.2f} m³/h · "
                f"管道总长 {sum(l.length_m for l in doc.links if l.kind == 'pipe'):.1f} m"
            )
            stats.setObjectName("summary")
            stats.setWordWrap(True)
            self.inspector_layout.addWidget(stats)
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
        title.setObjectName("inspectorTitle")
        self.inspector_layout.addWidget(title)
        form = QFormLayout()
        form.setVerticalSpacing(7)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        box = QWidget()
        box.setLayout(form)
        self.inspector_layout.addWidget(box)
        identifier_box = QLineEdit(item.id)
        identifier_box.textEdited.connect(lambda _, w=identifier_box: self._mark_changed(w))
        identifier_box.returnPressed.connect(self.apply_properties)
        self.fields["id"] = identifier_box
        form.addRow("编号", identifier_box)
        kinds = QComboBox()
        for value, text in (NODE_NAMES if kind == "node" else LINK_NAMES).items():
            kinds.addItem(text, value)
        kinds.setCurrentIndex(kinds.findData(item.kind))
        kinds.activated.connect(lambda _: self.apply_properties())
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
                w.activated.connect(lambda _, w=w: self._mark_changed(w))
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
            w = numeric(getattr(item, key), key)
            w.valueChanged.connect(lambda _, w=w: self._mark_changed(w))
            w.lineEdit().returnPressed.connect(self.apply_properties)
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
                w.toggled.connect(lambda _: self.pending_label and self.pending_label.show())
                self.fields[key] = w
                form.addRow(label, w)
            source = QComboBox()
            for value, text in [
                ("hypothetical", "假设值"),
                ("manufacturer", "厂家资料"),
                ("measured", "实测"),
                ("cad_geometry", "CAD 几何"),
            ]:
                source.addItem(text, value)
            source.setCurrentIndex(max(0, source.findData(item.source)))
            self.fields["source"] = source
            form.addRow("参数来源声明", source)
        note = QLineEdit(item.note)
        note.textEdited.connect(lambda _, w=note: self._mark_changed(w))
        self.fields["note"] = note
        form.addRow("备注", note)
        info = self._inspector_info(kind, item)
        if info:
            # Explicit line breaks (no word wrap) keep the scroll range stable.
            label = QLabel(info)
            label.setObjectName("summary")
            self.inspector_layout.addWidget(label)
        if kind == "link" and item.kind == "pump":
            label = QLabel("泵参考曲线（参考转速下）")
            label.setObjectName("sectionTitle")
            self.inspector_layout.addWidget(label)
            curve = CurveEditor([p.model_dump() for p in item.curve])
            curve.changed.connect(lambda: self.pending_label and self.pending_label.show())
            self.fields["curve"] = curve
            self.inspector_layout.addWidget(curve)
        actions = QHBoxLayout()
        actions.addWidget(primary("应用参数", self.apply_properties), 1)
        actions.addWidget(button("还原", self.show_inspector))
        holder = QWidget()
        holder.setLayout(actions)
        self.inspector_layout.addWidget(holder)
        self.pending_label = QLabel("有未应用的修改\n回车或点击“应用参数”生效")
        self.pending_label.setStyleSheet(f"color:{theme.HIGHLIGHT};")
        self.pending_label.hide()
        self.inspector_layout.addWidget(self.pending_label)
        self.inspector_layout.addStretch()

    def _inspector_info(self, kind, item):
        nodes = {n.id: n for n in self.document.nodes}
        if kind == "node":
            links = [l.id for l in self.document.links if item.id in (l.start, l.end)]
            shown = ", ".join(links[:6]) + (" …" if len(links) > 6 else "")
            text = f"连接：{shown if links else '无（孤立节点）'}"
            if item.kind == "junction":
                text += f"\n静水头（高程）{item.elevation_m:g} m"
            return text
        a, b = nodes.get(item.start), nodes.get(item.end)
        if a is None or b is None:
            return ""
        straight = math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        if item.kind == "pipe":
            area = math.pi * item.diameter_m**2 / 4
            return (
                f"端点直线距离 {straight:.2f} m\n（实际管长不得小于此值）\n"
                f"截面 {area * 1e4:.2f} cm² · 1 m/s 对应 {area * 3600:.2f} m³/h"
            )
        if item.kind == "pump":
            from backend.diagnostics import pump_curve_bep

            q, h, eta = pump_curve_bep(item)
            s = item.speed_ratio
            return (
                f"吸入 {item.start} → 出口 {item.end}\n"
                f"最高效率点（转速比 {s:g}）\n"
                f"Q≈{q * s:.1f} m³/h · H≈{h * s * s:.1f} m · η≈{eta * 100:.0f}%"
            )
        return f"阀门两端：{item.start} → {item.end}"

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
                    value[key] = spin_value(w)
                elif isinstance(w, QCheckBox):
                    value[key] = w.isChecked()
                elif isinstance(w, QComboBox):
                    value[key] = (
                        w.currentData() if key in ("kind", "source") else w.currentText()
                    )
                elif isinstance(w, CurveEditor):
                    value[key] = w.value()
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
            self.statusBar().showMessage(f"已应用 {parsed.id} 的参数", 4000)
        except Exception as exc:
            self.show_error(friendly_error(exc))

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
        except Exception as exc:
            self.show_error(friendly_error(exc))
            return
        ids = {("node", n.id) for n in doc.nodes} | {("link", l.id) for l in doc.links}
        flags = {}
        for message in errors:
            for token in message.replace("，", " ").replace("、", " ").split():
                for kind in ("node", "link"):
                    if (kind, token) in ids:
                        flags[(kind, token)] = "error"
        self.design_flags = flags
        self.editor.flags = flags
        self.editor.update()
        self.refresh_browser()
        if errors:
            self.log.appendPlainText("校验未通过：\n  " + "\n  ".join(errors))
            self.statusBar().showMessage(f"校验发现 {len(errors)} 个问题，已在图中用 ! 标出", 10000)
        else:
            self.log.appendPlainText("结构校验通过。几何可连通不等于工况一定可行。")
            self.statusBar().showMessage("结构校验通过", 6000)

    def edit_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("仿真设置与控制事件")
        outer = QVBoxLayout(dialog)
        body = QWidget()
        v = QVBoxLayout(body)
        form = QFormLayout()
        fields = {}
        for key, value in self.document.settings.model_dump().items():
            if key == "events":
                continue
            w = numeric(value, key)
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
        outer.addWidget(scrollable(body, "simulationSettingsScroll"), 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        fit_to_screen(dialog, 680, 700)
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
            + (" · 已暂停" if snapshot["paused"] else "")
        )
        active = c.get("running", 0) + c.get("queued", 0)
        self.tabs.setTabText(2, f"任务队列 ({active})" if active else "任务队列")
        self.pause_button.setText("继续队列" if snapshot["paused"] else "暂停队列")
        current = self.selected_job_id()
        jobs = snapshot["jobs"]
        self.jobs_table.setRowCount(len(jobs))
        for i, job in enumerate(jobs):
            state = job["state"]
            background, foreground = theme.STATE_COLORS.get(state, ("#ffffff", theme.INK))
            for j, text in enumerate(
                [
                    job["name"],
                    KIND_NAMES.get(job["kind"], job["kind"]),
                    STATE_NAMES[state],
                    "",
                    job["error"] or job["message"],
                    local_time(job["created_at"]),
                    duration_text(job.get("started_at"), job.get("finished_at"), state),
                ]
            ):
                item = self.jobs_table.item(i, j)
                if item is None:
                    item = QTableWidgetItem()
                    self.jobs_table.setItem(i, j, item)
                item.setText(text)
                item.setData(Qt.ItemDataRole.UserRole, job["id"])
                item.setToolTip(text if j in (0, 4) else "")
                if j == 2:
                    item.setBackground(QBrush(QColor(background)))
                    item.setForeground(QBrush(QColor(foreground)))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif j == 4 and state == "failed":
                    item.setForeground(QBrush(QColor(theme.DANGER)))
                else:
                    item.setForeground(QBrush(QColor(theme.INK)))
            bar = self.jobs_table.cellWidget(i, 3)
            if not isinstance(bar, QProgressBar):
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setTextVisible(True)
                self.jobs_table.setCellWidget(i, 3, bar)
            progress = float(job.get("progress") or 0)
            bar.setValue(round(100 * (1 if state == "succeeded" else progress)))
            bar.setFormat(f"{bar.value()}%")
            if job["id"] == current:
                self.jobs_table.selectRow(i)
        selected = self.history.currentData()
        self.history.blockSignals(True)
        self.history.clear()
        for r in runs:
            self.history.addItem(r["name"] + " · " + local_time(r["created_at"]), r["id"])
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
        if result.get("physics_result_id"):
            self.work(
                lambda: self.controller.physics_result(result["physics_result_id"]),
                self.physics_panel.display_result,
            )
        elif result.get("design_calibration_id"):
            controller = self.controller
            self.work(
                lambda: controller.calibration(result["design_calibration_id"]),
                self.display_calibration,
            )
        elif result.get("design_optimization_id"):
            controller = self.controller
            self.work(
                lambda: controller.optimization(result["design_optimization_id"]),
                self.show_optimization,
            )
        elif result.get("design_run_id"):
            self.load_result_id(result["design_run_id"])
        else:
            self.log.appendPlainText(job.get("error") or "任务尚无可读取的结果。")

    def show_optimization(self, value):
        """Readable table of the pump-combination / speed search."""
        self.details.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
        best = value.get("best")
        doc = best["document"] if best else self.document.model_dump(mode="json")
        pumps = [l["id"] for l in doc["links"] if l["kind"] == "pump"]
        baseline = value.get("baseline_summary", {}).get("energy_kwh") or 0
        rows = sorted(
            value.get("candidates", []),
            key=lambda c: (
                not c["feasible"],
                round(c.get("energy_kwh") or 1e18, 6),
                sum(c["mode"]),
                [not on for on in c["mode"]],
            ),
        )
        dialog = QDialog(self)
        dialog.setObjectName("optimizationDialog")
        dialog.setWindowTitle("低能耗方案搜索结果")
        layout = QVBoxLayout(dialog)
        title = QLabel("低能耗运行方案")
        title.setObjectName("inspectorTitle")
        layout.addWidget(title)
        feasible = [c for c in rows if c["feasible"]]
        if feasible and baseline > 0:
            top = feasible[0]
            running = "、".join(p for p, on in zip(pumps, top["mode"]) if on) or "—"
            saving = (1 - top["energy_kwh"] / baseline) * 100
            text = (
                f"共评估 {len(rows)} 个方案，{len(feasible)} 个满足供水、压力与储水约束。\n"
                f"推荐：运行 {running}，转速比 {top['speed_ratio']:.2f}；期间能耗 "
                f"{top['energy_kwh']:.2f} kWh，比当前设定 {baseline:.2f} kWh "
                f"{'节省' if saving >= 0 else '增加'} {abs(saving):.1f}%。"
            )
        else:
            text = f"共评估 {len(rows)} 个方案，没有方案同时满足全部约束。"
        summary = QLabel(text)
        summary.setObjectName("summary")
        layout.addWidget(summary)
        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels(["运行水泵", "转速比", "期间能耗 / kWh", "相对当前", "结论"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        for i in range(4):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        for r, c in enumerate(rows):
            energy = c.get("energy_kwh")
            values = [
                "、".join(p for p, on in zip(pumps, c["mode"]) if on) or "—",
                f"{c['speed_ratio']:.2f}",
                "—" if energy is None else f"{energy:.2f}",
                "—" if energy is None or baseline <= 0 else f"{(energy / baseline - 1) * 100:+.1f}%",
                ("✓ 可行" + ("（推荐）" if r == 0 and c["feasible"] else ""))
                if c["feasible"]
                else "✗ " + "、".join(c["reasons"][:2]),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 4:
                    item.setToolTip("；".join(c["reasons"]) or "满足约束")
                    item.setForeground(QBrush(QColor(theme.ACCENT_DARK if c["feasible"] else theme.MUTED)))
                if r == 0 and c["feasible"]:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setBackground(QBrush(QColor(theme.ACCENT_SOFT)))
                table.setItem(r, col, item)
        layout.addWidget(table, 1)
        note = QLabel(value.get("notice", ""))
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if best:
            view = buttons.addButton("查看推荐方案结果", QDialogButtonBox.ButtonRole.ActionRole)
            view.setObjectName("primary")
            view.clicked.connect(lambda: (dialog.accept(), self.display_result(best)))
            use = buttons.addButton("应用为新草稿", QDialogButtonBox.ButtonRole.ActionRole)
            use.clicked.connect(lambda: (dialog.accept(), self._use_document(best["document"])))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        fit_to_screen(dialog, 760, 560)
        self.optimization_dialog = dialog
        dialog.show()
        return dialog

    def _use_document(self, document):
        if self.confirm_discard():
            self.checkpoint()
            self.set_document(NetworkDocument.model_validate(document))
            self.tabs.setCurrentIndex(0)

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
        dialog.resize(760, 380)
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
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
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
        from backend.diagnostics import diagnose, format_metric

        NetworkDocument.model_validate(result["document"])
        self.play_timer.stop()
        self.play_button.setText("播放")
        self.current_result = result
        self.current_run_id = result.get("id")
        self.result_element = None
        self.result_canvas.set_document(NetworkDocument.model_validate(result["document"]))
        self.result_canvas.ranges = self._result_ranges(result)
        self.result_canvas.set_animate(self.flow_animation.isChecked())
        self.result_canvas.required_pressure = result["document"]["settings"]["required_pressure_m"]
        self.timeline.setRange(0, len(result["points"]) - 1)
        self.timeline.setValue(0)
        self.trend.points = result["points"]
        self.trend.network = self.result_canvas.document
        self.trend.pump_id = next(
            (l.id for l in self.trend.network.links if l.kind == "pump"), None
        )
        try:
            self.diagnostics = diagnose(result)
        except Exception as exc:  # diagnostics must never block viewing a result
            self.diagnostics = {"findings": [], "metrics": {}, "error": str(exc)}
            self.log.appendPlainText("诊断未完成：" + str(exc))
        findings = self.diagnostics["findings"]
        worst = {}
        for f in findings:
            key = (f["target_kind"], f["target"])
            if key not in worst:
                worst[key] = f["severity"]
        self.result_canvas.flags = worst
        self.findings.set_findings(findings)
        self.change_metric()
        self.show_frame(0)
        s = result["summary"]
        m = self.diagnostics.get("metrics", {})
        pressures = [p["min_pressure_m"] for p in result["points"] if p.get("min_pressure_m") is not None]
        required = result["document"]["settings"]["required_pressure_m"]
        self.kpis["energy"].set(f"{s['energy_kwh']:.3f}")
        self.kpis["cost"].set(f"{s['cost_yuan']:.2f}")
        self.kpis["delivered"].set(f"{s['delivered_m3']:.2f}")
        self.kpis["unmet"].set(
            f"{s['unmet_m3']:.3f}", theme.DANGER if s["unmet_m3"] > 0.005 else theme.ACCENT_DARK
        )
        self.kpis["specific"].set(format_metric(m.get("specific_energy_kwh_m3"), 3))
        self.kpis["efficiency"].set(
            format_metric(m.get("wire_to_water_efficiency"), 1, 100),
            tooltip="管网获得的水力功 ÷ 泵组电输入（含电机与驱动损失）",
        )
        low = min(pressures) if pressures else None
        self.kpis["pressure"].set(
            format_metric(low, 2),
            theme.DANGER if low is not None and low < required - 0.05 else None,
            f"要求 ≥ {required:g} m",
        )
        errors, warnings = m.get("error_count", 0), m.get("warning_count", 0)
        if "error" in self.diagnostics:
            self.kpis["health"].set("诊断未完成", theme.WARNING, self.diagnostics["error"])
        elif errors:
            self.kpis["health"].set(f"{errors} 严重 · {warnings} 警告", theme.DANGER)
        elif warnings:
            self.kpis["health"].set(f"{warnings} 项警告", theme.WARNING)
        else:
            self.kpis["health"].set("正常", theme.ACCENT_DARK)
        meta = result.get("meta", {})
        self.result_summary.setText(
            f"<b>{result['document']['name']}</b>　·　{meta.get('solver', '')} / "
            f"{meta.get('headloss', '')} / {meta.get('demand_model', '')}"
            f"{' 已收敛' if meta.get('converged', True) else ' 未收敛'}　·　"
            f"峰值功率 {s.get('peak_power_kw', 0):.2f} kW　·　"
            f"{len(result['points'])} 个报告时刻"
        )
        for warning in result["warnings"]:
            self.log.appendPlainText("运行提示：" + warning)
        self.update_element_panel()
        self.tabs.setCurrentIndex(1)

    @staticmethod
    def _result_ranges(result):
        doc = result["document"]
        junctions = {n["id"] for n in doc["nodes"] if n["kind"] == "junction"}
        lengths = {l["id"]: l["length_m"] for l in doc["links"] if l["kind"] == "pipe"}
        pressures, flows, velocities, losses = [], [], [], []
        for p in result["points"]:
            for k, v in p["nodes"].items():
                if k in junctions and v.get("pressure_m") is not None:
                    pressures.append(v["pressure_m"])
            for k, v in p["links"].items():
                flows.append(abs(v.get("flow_m3h") or 0))
                velocities.append(abs(v.get("velocity_ms") or 0))
                if k in lengths and lengths[k] > 0:
                    losses.append(abs(v.get("headloss_m") or 0) / lengths[k] * 1000)
        ranges = {}
        if pressures:
            ranges["pressure"] = (min(pressures), max(pressures))
        if flows:
            ranges["flow"] = (0.0, max(flows))
        if velocities:
            ranges["velocity"] = (0.0, max(velocities))
        if losses:
            ranges["headloss"] = (0.0, max(losses))
        return ranges

    def show_frame(self, index):
        if not self.current_result:
            return
        point = self.current_result["points"][index]
        self.result_canvas.point = point
        self.result_canvas.update()
        self.time_label.setText(f"{point['hour']:.2f} h")
        self.trend.cursor = index
        if self.trend.key == "profile":
            self.trend.profile = self.hydraulic_profile()
        self.trend.update()
        self.update_element_panel()

    def toggle_play(self):
        if not self.current_result:
            return
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play_button.setText("播放")
        else:
            if self.timeline.value() == self.timeline.maximum():
                self.timeline.setValue(0)
            self.play_timer.start(self.play_speed.currentData())
            self.play_button.setText("暂停")

    def advance_frame(self):
        if self.timeline.value() >= self.timeline.maximum():
            self.play_timer.stop()
            self.play_button.setText("播放")
        else:
            self.timeline.setValue(self.timeline.value() + 1)

    def toggle_result_dimension(self):
        self.result_canvas.set_dimension("2d" if self.result_canvas.dimension == "3d" else "3d")

    def change_metric(self):
        self.trend.key = self.trend_metric.currentData()
        self.trend.label = self.trend_metric.currentText()
        self.trend.series = None
        self.trend.reference = None
        if self.trend.key == "min_pressure_m" and self.current_result:
            self.trend.reference = self.current_result["document"]["settings"]["required_pressure_m"]
        if self.trend.key == "profile":
            self.trend.profile = self.hydraulic_profile()
        if self.trend.key == "element":
            series = self.element_series()
            if series:
                self.trend.label, self.trend.series, self.trend.reference = series
            else:
                self.trend.label = "所选元素时序：在结果图中点击节点或管段"
                self.trend.series = []
        self.trend.update()

    def element_series(self):
        if not self.current_result or not self.result_element:
            return None
        kind, identifier = self.result_element
        points = self.current_result["points"]
        if kind == "node":
            values = [p["nodes"].get(identifier, {}).get("pressure_m") or 0 for p in points]
            node = next((n for n in self.result_canvas.document.nodes if n.id == identifier), None)
            reference = (
                self.current_result["document"]["settings"]["required_pressure_m"]
                if node is not None and node.kind == "junction" and node.demand_m3h > 0
                else None
            )
            return f"{identifier} 压力 / m", values, reference
        values = [p["links"].get(identifier, {}).get("flow_m3h") or 0 for p in points]
        return f"{identifier} 流量 / m³/h", values, None

    def hydraulic_profile(self):
        """Stations from the nearest source (by pipe length) to the selected node."""
        import networkx as nx

        if not self.current_result:
            return None
        doc = self.result_canvas.document
        point = self.current_result["points"][self.timeline.value()]
        nodes = {n.id: n for n in doc.nodes}
        target = None
        if self.result_element:
            kind, identifier = self.result_element
            if kind == "node":
                target = identifier
            else:
                link = next((l for l in doc.links if l.id == identifier), None)
                if link is not None:
                    target = link.end
        if target is None:
            candidates = [
                (v["pressure_m"], k)
                for k, v in point["nodes"].items()
                if k in nodes and nodes[k].kind == "junction" and nodes[k].demand_m3h > 0
                and v.get("pressure_m") is not None
            ]
            if not candidates:
                return None
            target = min(candidates)[1]
        graph = nx.Graph()
        for l in doc.links:
            if not point["links"].get(l.id, {}).get("active", l.enabled):
                continue
            weight = l.length_m if l.kind == "pipe" else 0.0
            if not graph.has_edge(l.start, l.end) or graph[l.start][l.end]["weight"] > weight:
                graph.add_edge(l.start, l.end, weight=weight, link=l)
        # Profiles start at the supply (reservoirs); tanks are used only when there is none.
        sources = [n.id for n in doc.nodes if n.kind == "reservoir" and n.id in graph]
        if not sources or not any(nx.has_path(graph, s, target) for s in sources if target in graph):
            sources = [n.id for n in doc.nodes if n.kind in ("reservoir", "tank") and n.id in graph]
        if not sources or target not in graph:
            return {"title": f"沿程水力剖面 · {target}", "stations": [],
                    "message": "未找到从水源到该节点的连通路径"}
        try:
            _, path = nx.multi_source_dijkstra(graph, sources, target=target, weight="weight")
        except nx.NetworkXNoPath:
            return {"title": f"沿程水力剖面 · {target}", "stations": [],
                    "message": "未找到从水源到该节点的连通路径"}
        stations, distance, pump = [], 0.0, False
        for i, nid in enumerate(path):
            if i:
                edge = graph[path[i - 1]][nid]
                distance += edge["weight"]
                pump = edge["link"].kind == "pump"
            n = nodes[nid]
            data = point["nodes"].get(nid, {})
            stations.append({
                "id": nid,
                "distance": distance,
                "elevation": n.elevation_m,
                "head": data.get("head_m", n.elevation_m),
                "pressure": None if n.kind == "reservoir" else data.get("pressure_m"),
                "demand": n.kind == "junction" and n.demand_m3h > 0,
                "pump": pump,
            })
        required = self.current_result["document"]["settings"]["required_pressure_m"]
        return {
            "title": f"沿程水力剖面 · {path[0]} → {target} · {point['hour']:.2f} h",
            "stations": stations,
            "required": required,
        }

    def change_result_color(self):
        self.result_canvas.color_metric = self.color_mode.currentData()
        self.result_canvas.update()

    def result_selection(self, kind, identifier):
        if not self.current_result:
            return
        self.result_element = (kind, identifier)
        self.result_canvas.selection = (kind, identifier)
        if kind == "link" and any(
            l.id == identifier and l.kind == "pump" for l in self.result_canvas.document.links
        ):
            self.trend.pump_id = identifier
            if self.trend_metric.currentData() != "element":
                self.trend_metric.setCurrentIndex(self.trend_metric.findData("pump_curve"))
        if self.trend_metric.currentData() in ("element", "profile"):
            self.change_metric()
        self.trend.update()
        self.result_canvas.update()
        self.update_element_panel()
        self.statusBar().showMessage(f"已选择 {identifier}；右侧显示当前时刻数值", 6000)

    def update_element_panel(self):
        table = self.element_table
        if not self.current_result or not self.result_element:
            self.element_title.setText("所选元素")
            table.setRowCount(1)
            table.setItem(0, 0, QTableWidgetItem("提示"))
            table.setItem(0, 1, QTableWidgetItem("在结果图中点击节点、管道或泵"))
            return
        kind, identifier = self.result_element
        point = self.current_result["points"][self.timeline.value()]
        data = point["nodes" if kind == "node" else "links"].get(identifier, {})
        doc = self.result_canvas.document
        element = next(
            (x for x in (doc.nodes if kind == "node" else doc.links) if x.id == identifier), None
        )
        names = NODE_NAMES if kind == "node" else LINK_NAMES
        self.element_title.setText(
            f"{identifier} · {names.get(element.kind, '') if element else ''} · {point['hour']:.2f} h"
        )
        rows = []
        if kind == "node":
            rows += [
                ("压力", data.get("pressure_m"), "m", 3),
                ("总水头", data.get("head_m"), "m", 3),
                ("实际供水", data.get("demand_m3h"), "m³/h", 3),
                ("期望需水", data.get("requested_m3h"), "m³/h", 3),
            ]
            if data.get("tank_level_m") is not None:
                rows.append(("水箱液位", data["tank_level_m"], "m", 3))
        else:
            rows += [
                ("流量", data.get("flow_m3h"), "m³/h", 3),
                ("水头损失", data.get("headloss_m"), "m", 3),
                ("状态", "运行" if data.get("active", True) else "停止 / 关闭", "", 0),
            ]
            if element is not None and element.kind == "pipe":
                rows.append(("流速", data.get("velocity_ms"), "m/s", 3))
            if element is not None and element.kind == "pump":
                rows += [
                    ("转速", data.get("rpm"), "rpm", 0),
                    ("扬程", data.get("head_m"), "m", 3),
                    ("轴功率", data.get("shaft_kw"), "kW", 3),
                    ("电功率", data.get("power_kw"), "kW", 3),
                    ("泵效率", (data.get("efficiency") or 0) * 100, "%", 1),
                ]
        related = [
            f for f in (self.diagnostics or {}).get("findings", []) if f["target"] == identifier
        ]
        table.setRowCount(len(rows) + len(related))
        for r, (label, value, unit, digits) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(label))
            if isinstance(value, (int, float)):
                text = f"{value:.{digits}f} {unit}".strip()
            else:
                text = "—" if value is None else str(value)
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, 1, item)
        for i, f in enumerate(related):
            r = len(rows) + i
            label = QTableWidgetItem("诊断")
            label.setForeground(QBrush(QColor(theme.SEVERITY_COLORS[f["severity"]])))
            table.setItem(r, 0, label)
            item = QTableWidgetItem(f["message"])
            item.setToolTip(f["message"])
            table.setItem(r, 1, item)

    def pick_finding(self, kind, identifier, hour):
        if not self.current_result:
            return
        points = self.current_result["points"]
        index = min(range(len(points)), key=lambda i: abs(points[i]["hour"] - hour))
        self.timeline.setValue(index)
        self.result_selection(kind, identifier)

    def use_result_document(self):
        if self.current_result and self.confirm_discard():
            self.checkpoint()
            self.set_document(NetworkDocument.model_validate(self.current_result["document"]))
            self.tabs.setCurrentIndex(0)

    def import_cad(self, path=None):
        if not self.confirm_discard():
            return
        if not path:
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
                lambda content: (
                    Path(path).write_text(content, encoding="utf-8"),
                    self.statusBar().showMessage("已导出 INP：" + path, 5000),
                ),
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

    # ------------------------------------------------------- recent / files
    def recent_files(self):
        value = self.settings.value("recent", [])
        if isinstance(value, str):
            value = [value]
        return [v for v in (value or []) if v]

    def add_recent(self, path):
        path = str(Path(path).resolve())
        files = [path] + [p for p in self.recent_files() if p != path]
        self.settings.setValue("recent", files[:MAX_RECENT])
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        menu = getattr(self, "recent_menu", None)
        if menu is None:
            return
        menu.clear()
        files = self.recent_files()
        for path in files:
            action = menu.addAction(Path(path).name)
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(lambda checked=False, p=path: self.open_recent(p))
        if not files:
            empty = menu.addAction("（暂无）")
            empty.setEnabled(False)
        else:
            menu.addSeparator()
            menu.addAction("清除列表", lambda: (self.settings.remove("recent"),
                                                self._refresh_recent_menu()))

    def open_recent(self, path):
        if not Path(path).exists():
            self.show_error("文件不存在：" + path)
            self.settings.setValue("recent", [p for p in self.recent_files() if p != path])
            self._refresh_recent_menu()
            return
        if self.confirm_discard():
            self.load_project(Path(path))

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if urls and Path(urls[0].toLocalFile()).suffix.lower() in (".json", ".dxf"):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = Path(event.mimeData().urls()[0].toLocalFile())
        event.acceptProposedAction()
        self.open_path(path)

    def open_path(self, path):
        """Open a project, a result JSON or a DXF depending on the file."""
        path = Path(path)
        if path.suffix.lower() == ".dxf":
            self.import_cad(str(path))
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self.show_error(str(exc))
            return
        if isinstance(value, dict) and "points" in value and "document" in value:
            self.display_result(value)
        elif self.confirm_discard():
            self.load_project(path)

    def export_png(self, widget, default):
        path, _ = QFileDialog.getSaveFileName(self, "导出图片", default, "PNG (*.png)")
        if path:
            widget.grab().save(path)
            self.statusBar().showMessage("已导出图片：" + path, 5000)

    def export_diagnostics(self):
        if not self.diagnostics:
            self.show_error("请先加载一个计算结果")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出诊断报告", "diagnostics.csv", "CSV (*.csv)")
        if not path:
            return
        from backend.diagnostics import SEVERITY_NAMES

        with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["级别", "类别", "元素类型", "元素", "首次时刻_h", "出现次数", "说明", "极值", "阈值"])
            for d in self.diagnostics["findings"]:
                writer.writerow([
                    SEVERITY_NAMES[d["severity"]], d["category"], d["target_kind"], d["target"],
                    d["first_hour"], d["count"], d["message"], d["value"], d["limit"],
                ])
            for key, value in self.diagnostics.get("metrics", {}).items():
                writer.writerow(["指标", key, "", "", "", "", value, "", ""])
        self.statusBar().showMessage("已导出诊断报告：" + path, 5000)

    def show_shortcuts(self):
        QMessageBox.information(
            self,
            "快捷键",
            "管网设计（画布获得焦点时）\n"
            "  V 选择 / 移动　J 节点　R 水源　T 水箱\n"
            "  L 管道　P 水泵　K 阀门　Esc 取消连接并回到选择\n"
            "  Delete / Backspace 删除所选　F 适应视图\n\n"
            "画布\n"
            "  左键拖动节点（二维）或旋转（三维）；拖动空白处、右键或中键平移\n"
            "  ⌘/Ctrl + 滚轮、触控板双指捏合：以光标为中心缩放\n\n"
            "全局\n"
            "  ⌘/Ctrl+N 新建　⌘/Ctrl+O 打开　⌘/Ctrl+S 保存　⌘/Ctrl+Z 撤销\n"
            "  ⌘/Ctrl+R 运行仿真　⌘/Ctrl+0 适应视图　⌘/Ctrl+ +/- 缩放\n\n"
            "可把工程 JSON、结果 JSON 或 DXF 文件直接拖进窗口打开。",
        )

    def show_about(self):
        QMessageBox.about(
            self,
            "关于 FLOWSTATE",
            "<b>FLOWSTATE</b> · 离心泵与二维 / 三维管网工作台<br><br>"
            "EPANET 2.2（WNTR）/ Darcy–Weisbach / 压力驱动需水；全部计算在本机完成。<br>"
            "运行诊断基于经验阈值，示例参数为假设值，结论需实测数据校准。<br><br>"
            "MIT 许可证；第三方组件见 THIRD_PARTY_NOTICES.md。",
        )

    def restore_window_state(self):
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

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
        try:
            self.settings.setValue("window/geometry", self.saveGeometry())
        except Exception:
            pass
        self.poll_timer.stop()
        self.play_timer.stop()
        self.result_canvas.set_animate(False)
        self.controller.close()
        self.pool.waitForDone(32000)
        event.accept()
