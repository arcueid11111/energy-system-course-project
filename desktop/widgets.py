"""Small reusable widgets for the main window."""

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme

NODE_TEXT = {"junction": "需水 / 连接节点", "reservoir": "定水头水源", "tank": "水箱"}
LINK_TEXT = {"pipe": "管道", "pump": "离心泵", "valve": "调节阀"}


class KpiCard(QFrame):
    def __init__(self, title, unit="", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        layout.addWidget(self.title)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.value = QLabel("—")
        self.value.setObjectName("cardValue")
        row.addWidget(self.value)
        self.unit = QLabel(unit)
        self.unit.setObjectName("cardUnit")
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        layout.addLayout(row)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, value, color=None, tooltip=""):
        self.value.setText(value)
        self.value.setStyleSheet(f"color:{color};" if color else "")
        self.setToolTip(tooltip)


class ElementBrowser(QWidget):
    """Searchable tree of all nodes and links in the document."""

    picked = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)
        title = QLabel("元素列表")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("搜索编号 / 备注…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.apply_filter)
        layout.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["编号", "摘要"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemActivated.connect(self._clicked)
        layout.addWidget(self.tree, 1)
        self._items = {}
        self._expanded = {}
        self.setMinimumWidth(180)

    def set_document(self, doc, flags=None):
        flags = flags or {}
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            self._expanded[group.text(0).split(" ")[0]] = group.isExpanded()
        selected = self.current()
        self.tree.clear()
        self._items = {}
        groups = [
            ("node", kind, NODE_TEXT[kind], [n for n in doc.nodes if n.kind == kind])
            for kind in ("reservoir", "tank", "junction")
        ] + [
            ("link", kind, LINK_TEXT[kind], [l for l in doc.links if l.kind == kind])
            for kind in ("pump", "valve", "pipe")
        ]
        for element, kind, text, items in groups:
            if not items:
                continue
            group = QTreeWidgetItem([f"{text} ({len(items)})", ""])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            self.tree.addTopLevelItem(group)
            for item in items:
                child = QTreeWidgetItem([item.id, self._summary(element, item)])
                child.setData(0, Qt.ItemDataRole.UserRole, (element, item.id))
                child.setToolTip(1, item.note or child.text(1))
                severity = flags.get((element, item.id))
                if severity:
                    child.setForeground(0, QBrush(QColor(theme.SEVERITY_COLORS[severity])))
                if element == "link" and not item.enabled:
                    child.setForeground(1, QBrush(QColor("#9aaba2")))
                group.addChild(child)
                self._items[(element, item.id)] = child
            group.setExpanded(self._expanded.get(text, True))
        if selected:
            self.select(*selected)
        self.apply_filter(self.filter.text())

    @staticmethod
    def _summary(element, item):
        if element == "node":
            if item.kind == "reservoir":
                return f"H {item.source_head_m:g} m"
            if item.kind == "tank":
                return f"液位 {item.tank_level_m:g} m"
            return f"{item.demand_m3h:g} m³/h" if item.demand_m3h else f"z {item.z:g}"
        text = f"{item.start}→{item.end}"
        if item.kind == "pipe":
            text += f" · DN{item.diameter_m * 1000:g}"
        elif item.kind == "pump":
            text += f" · ×{item.speed_ratio:g}"
        else:
            text += f" · {item.opening * 100:g}%"
        return text + ("" if item.enabled else " · 停用")

    def apply_filter(self, text):
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            visible = 0
            for j in range(group.childCount()):
                child = group.child(j)
                match = not text or text in child.text(0).lower() or text in child.toolTip(1).lower()
                child.setHidden(not match)
                visible += match
            group.setHidden(visible == 0)

    def current(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def select(self, kind, identifier):
        item = self._items.get((kind, identifier))
        self.tree.blockSignals(True)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        else:
            self.tree.clearSelection()
        self.tree.blockSignals(False)

    def _clicked(self, item, column=0):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.picked.emit(*data)


class CurvePreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.error = ""
        self.setMinimumHeight(150)

    def paintEvent(self, event):
        from .canvas import draw_pump_curve

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        p.setPen(QColor(theme.LINE))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        r = QRectF(58, 22, max(10, self.width() - 74), max(10, self.height() - 52))
        if self.rows:
            draw_pump_curve(p, r, None, 1.0, None, curve=[(q, h, kw) for q, h, kw, _ in self.rows])
        if self.error:
            p.setPen(QColor(theme.DANGER))
            p.drawText(QRectF(8, 2, self.width() - 16, 18), Qt.AlignmentFlag.AlignLeft, self.error)
        else:
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(8, 2, self.width() - 16, 18), Qt.AlignmentFlag.AlignLeft,
                       "H–Q（实线）与效率 η（虚线），参考转速")


class CurveEditor(QWidget):
    """Table editor for pump reference curves with a live H–Q / η preview."""

    changed = Signal()
    COLUMNS = [("flow_m3h", "Q m³/h"), ("head_m", "H m"), ("shaft_kw", "P 轴 kW"),
               ("npsh3_m", "NPSH3 m")]

    def __init__(self, points, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([c[1] for c in self.COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("＋ 添加点")
        add.clicked.connect(self.add_row)
        remove = QPushButton("－ 删除所选")
        remove.clicked.connect(self.remove_row)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        self.preview = CurvePreview()
        layout.addWidget(self.preview)
        for p in points:
            self._append([p[k] for k, _ in self.COLUMNS])
        self.table.itemChanged.connect(self._edited)
        self._edited()

    def _append(self, values):
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, v in enumerate(values):
            item = QTableWidgetItem(f"{float(v):g}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, c, item)

    def add_row(self):
        rows = self.rows(strict=False)
        if rows:
            q, h, kw, n = rows[-1]
            values = [q + max(1.0, q * 0.2), max(0.1, h * 0.7), kw, n * 1.2]
        else:
            values = [0, 10, 1, 1]
        self.table.blockSignals(True)
        self._append(values)
        self.table.blockSignals(False)
        self._edited()

    def remove_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)
        self._edited()

    def rows(self, strict=True):
        out = []
        for r in range(self.table.rowCount()):
            try:
                out.append(tuple(float(self.table.item(r, c).text()) for c in range(4)))
            except (AttributeError, ValueError):
                if strict:
                    raise ValueError(f"泵曲线第 {r + 1} 行含非数字")
        return out

    def value(self):
        return [dict(zip([k for k, _ in self.COLUMNS], row)) for row in self.rows()]

    def _edited(self, *args):
        try:
            rows = self.rows()
            error = ""
            if len(rows) < 3:
                error = "至少 3 个点"
            elif rows[0][0] != 0:
                error = "第一点流量应为 0"
            elif any(a[0] >= b[0] or a[1] <= b[1] for a, b in zip(rows, rows[1:])):
                error = "流量须递增、扬程须递减"
            elif any(r[2] <= 0 or r[3] <= 0 for r in rows):
                error = "轴功率与 NPSH3 须为正"
        except ValueError as exc:
            rows, error = self.rows(strict=False), str(exc)
        self.preview.rows = rows
        self.preview.error = error
        self.preview.update()
        self.changed.emit()


class FindingList(QTableWidget):
    """Diagnostics table; emits the element and hour of a clicked finding."""

    picked = Signal(str, str, float)

    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["级别", "元素", "首次", "说明"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setWordWrap(True)
        header = self.horizontalHeader()
        for i in range(3):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.cellClicked.connect(self._clicked)
        self.findings = []

    def set_findings(self, findings):
        from backend.diagnostics import SEVERITY_NAMES

        self.findings = findings
        self.setRowCount(max(1, len(findings)))
        if not findings:
            for c, text in enumerate(["✓", "", "", "未发现越限：压力、流速、泵工况、NPSH 与电机负荷均在经验范围内"]):
                item = QTableWidgetItem(text)
                item.setForeground(QBrush(QColor("#22734d")))
                self.setItem(0, c, item)
            return
        for r, f in enumerate(findings):
            severity = QTableWidgetItem("● " + SEVERITY_NAMES[f["severity"]])
            severity.setForeground(QBrush(QColor(theme.SEVERITY_COLORS[f["severity"]])))
            self.setItem(r, 0, severity)
            self.setItem(r, 1, QTableWidgetItem(f"{f['target']} · {f['category']}"))
            self.setItem(r, 2, QTableWidgetItem(f"{f['first_hour']:.2f} h"))
            text = f["message"] + (f"（{f['count']} 个时刻）" if f["count"] > 1 else "")
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            self.setItem(r, 3, item)
        self.resizeRowsToContents()

    def _clicked(self, row, column):
        if 0 <= row < len(self.findings):
            f = self.findings[row]
            self.picked.emit(f["target_kind"], f["target"], float(f["first_hour"]))
