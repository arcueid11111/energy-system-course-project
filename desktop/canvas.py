import math

import time

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QBrush,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QToolTip, QWidget

from . import theme
from .scrolling import zoom_wheel

# Sequential scale used for pressure / velocity (low → high).
SCALE = ["#c8553d", "#e39b3b", "#a8c26a", "#3c9a86", "#2a5f86"]
FLOW_SCALE = ["#b9d3e6", "#6fa6cf", "#3b78b4", "#1f4f8a"]


def ramp(stops, t):
    """Interpolate a list of hex colours at t in [0, 1]."""
    t = max(0.0, min(1.0, t if math.isfinite(t) else 0.0))
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    a, b = QColor(stops[i]), QColor(stops[i + 1])
    f = pos - i
    return QColor(
        round(a.red() + (b.red() - a.red()) * f),
        round(a.green() + (b.green() - a.green()) * f),
        round(a.blue() + (b.blue() - a.blue()) * f),
    )


METRIC_TEXT = {
    "pressure": ("节点压力", "m"),
    "flow": ("管道流量", "m³/h"),
    "velocity": ("管道流速", "m/s"),
    "headloss": ("单位水头损失", "m/km"),
}
# Colour modes: what nodes and links show.
NODE_METRIC = {"pressure": "pressure", "combined": "pressure"}
LINK_METRIC = {"flow": "flow", "velocity": "velocity", "headloss": "headloss",
               "combined": "velocity"}
LINK_STOPS = {"flow": FLOW_SCALE, "velocity": SCALE[::-1], "headloss": SCALE[::-1]}


def link_value(link, data, metric):
    if metric == "flow":
        return abs(data.get("flow_m3h") or 0)
    if metric == "velocity":
        if link is not None and link.kind != "pipe":
            return None
        return abs(data.get("velocity_ms") or 0)
    if metric == "headloss":
        if link is None or link.kind != "pipe" or link.length_m <= 0:
            return None
        return abs(data.get("headloss_m") or 0) / link.length_m * 1000
    return None


class NetworkCanvas(QWidget):
    selected = Signal(str, str)
    add_requested = Signal(str, float, float)
    connect_requested = Signal(str, str, str)
    begin_edit = Signal()
    changed = Signal()
    delete_requested = Signal()
    deselected = Signal()
    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)

    def __init__(self, document, editable=True, parent=None):
        super().__init__(parent)
        self.document = document
        self.editable = editable
        self.mode = "select"
        self.dimension = "2d"
        self.selection = None
        self.hover = None
        self.connection_start = None
        self.point = None
        self.color_metric = "pressure"
        self.ranges = {}
        self.required_pressure = None
        self.flags = {}
        self.zoom = 1.0
        self.pan = QPointF()
        self.yaw = math.radians(35)
        self.pitch = math.radians(30)
        self._drag = None
        self._last = None
        self._mouse = None
        self._moved = False
        self.snap = False
        self.snap_step = 1.0
        self.show_labels = True
        self.animate = False
        self.phase = 0.0
        self._tick = time.monotonic()
        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._advance_animation)
        self._view_center = None
        self._view_scale = None
        self.reindex()
        self.setMinimumSize(420, 280)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

    # ------------------------------------------------------------------ model
    def set_document(self, document):
        self.document = document
        self.selection = None
        self.hover = None
        self.connection_start = None
        self.point = None
        self.flags = {}
        self.fit()
        self.update()

    def fit(self):
        self.zoom = 1.0
        self.pan = QPointF()
        self._view_center = None
        self._view_scale = None
        self.reindex()
        self.update()
        self.zoom_changed.emit(self.zoom)

    def zoom_by(self, factor, anchor=None):
        """Zoom keeping the screen point ``anchor`` (default: centre) fixed."""
        old = self.zoom
        self.zoom = max(0.05, min(40, self.zoom * factor))
        factor = self.zoom / old
        if anchor is not None:
            c = QPointF(self.width() / 2, self.height() / 2)
            offset = anchor - c - self.pan
            self.pan = anchor - c - offset * factor
        else:
            self.pan *= factor
        self.update()
        self.zoom_changed.emit(self.zoom)

    def reindex(self):
        self._nodes = {n.id: n for n in self.document.nodes}
        self._parallels = {}
        for l in self.document.links:
            self._parallels.setdefault(tuple(sorted((l.start, l.end))), []).append(l)

    def set_dimension(self, dimension):
        self.dimension = dimension
        self.connection_start = None
        self.fit()

    def center(self):
        if self._view_center is not None:
            return self._view_center
        nodes = self.document.nodes
        if not nodes:
            return (0.0, 0.0, 0.0)
        self._view_center = tuple(
            (min(getattr(n, k) for n in nodes) + max(getattr(n, k) for n in nodes)) / 2
            for k in ("x", "y", "z")
        )
        return self._view_center

    def project_world(self, p):
        c = self.center()
        x, y, z = (p[i] - c[i] for i in range(3))
        if self.dimension == "2d":
            return x, -y, 0.0
        a, b = self.yaw, self.pitch
        u = x * math.cos(a) - y * math.sin(a)
        depth = x * math.sin(a) + y * math.cos(a)
        return u, -(z * math.cos(b) - depth * math.sin(b)), depth * math.cos(b) + z * math.sin(b)

    def base_scale(self):
        if self._view_scale is not None:
            return self._view_scale
        projected = [self.project_world((n.x, n.y, n.z)) for n in self.document.nodes]
        if not projected:
            return 6.0
        xs = [p[0] for p in projected]
        ys = [p[1] for p in projected]
        self._view_scale = min(
            max(80, self.width() - 140) / max(15, max(xs) - min(xs)),
            max(80, self.height() - 120) / max(15, max(ys) - min(ys)),
        )
        return self._view_scale

    def screen(self, p):
        x, y, _ = self.project_world(p)
        scale = self.base_scale() * self.zoom
        return QPointF(
            self.width() / 2 + self.pan.x() + x * scale,
            self.height() / 2 + self.pan.y() + y * scale,
        )

    def _snap(self, value):
        step = self.snap_step or 1.0
        return round(value / step) * step

    def world_xy(self, p):
        c = self.center()
        scale = self.base_scale() * self.zoom
        x = (p.x() - self.width() / 2 - self.pan.x()) / scale + c[0]
        y = -(p.y() - self.height() / 2 - self.pan.y()) / scale + c[1]
        return (self._snap(x) if self.snap else x, self._snap(y) if self.snap else y)

    def line_points(self, link):
        nodes = self._nodes
        if link.start not in nodes or link.end not in nodes:
            return []
        a, b = nodes[link.start], nodes[link.end]
        geometry = link.geometry or [(a.x, a.y, a.z), (b.x, b.y, b.z)]
        geometry = [(a.x, a.y, a.z), *geometry[1:-1], (b.x, b.y, b.z)]
        points = [self.screen(p) for p in geometry]
        parallels = self._parallels.get(tuple(sorted((link.start, link.end))), [])
        if len(parallels) > 1 and len(points) == 2:
            index = parallels.index(link) - (len(parallels) - 1) / 2
            dx = points[1].x() - points[0].x()
            dy = points[1].y() - points[0].y()
            size = max(1, math.hypot(dx, dy))
            mid = (points[0] + points[1]) / 2 + QPointF(-dy / size, dx / size) * index * 48
            points = [points[0], mid, points[1]]
        return points

    def hit(self, pos):
        self.reindex()
        for n in reversed(self.document.nodes):
            q = self.screen((n.x, n.y, n.z))
            if math.hypot(pos.x() - q.x(), pos.y() - q.y()) < 13:
                return ("node", n.id)
        best = None
        distance = 12
        for l in self.document.links:
            pts = self.line_points(l)
            for a, b in zip(pts, pts[1:]):
                dx, dy = b.x() - a.x(), b.y() - a.y()
                norm = dx * dx + dy * dy
                t = (
                    max(0, min(1, ((pos.x() - a.x()) * dx + (pos.y() - a.y()) * dy) / norm))
                    if norm
                    else 0
                )
                d = math.hypot(pos.x() - a.x() - t * dx, pos.y() - a.y() - t * dy)
                if d < distance:
                    best = ("link", l.id)
                    distance = d
        return best

    # --------------------------------------------------------------- colours
    def _range(self, metric):
        lo, hi = self.ranges.get(metric, (None, None))
        if lo is None:
            values = []
            if self.point:
                if metric == "pressure":
                    values = [
                        v["pressure_m"]
                        for k, v in self.point["nodes"].items()
                        if self._nodes.get(k) is not None
                        and self._nodes[k].kind == "junction"
                        and v.get("pressure_m") is not None
                    ]
                else:
                    links = {l.id: l for l in self.document.links}
                    values = [
                        x
                        for k, v in self.point["links"].items()
                        if (x := link_value(links.get(k), v, metric)) is not None
                    ]
            lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
            if metric != "pressure":
                lo = 0.0
        if hi - lo < 1e-9:
            hi = lo + 1.0
        return lo, hi

    def _link_style(self, link, data):
        """Colour and width for a link in the current display mode."""
        base = QColor(theme.ACCENT if link.enabled else "#b6c5bf")
        width = 3.0 if link.kind == "pipe" else 4.0
        metric = LINK_METRIC.get(self.color_metric)
        if data and metric and data.get("active", True):
            value = link_value(link, data, metric)
            if value is not None:
                lo, hi = self._range(metric)
                t = (value - lo) / (hi - lo)
                base = ramp(LINK_STOPS[metric], t)
                width = 2.0 + 5.0 * max(0.0, min(1.0, t))
        return base, width

    def _pressure_gradient(self, link, pts):
        """Pen brush interpolating the two end-node pressure colours along a pipe."""
        if not self.point or self.color_metric != "pressure":
            return None
        a = self.point["nodes"].get(link.start, {})
        b = self.point["nodes"].get(link.end, {})
        na, nb = self._nodes.get(link.start), self._nodes.get(link.end)
        if a.get("pressure_m") is None or b.get("pressure_m") is None or na is None or nb is None:
            return None
        lo, hi = self._range("pressure")
        gradient = QLinearGradient(pts[0], pts[-1])
        gradient.setColorAt(0, ramp(SCALE, (a["pressure_m"] - lo) / (hi - lo)))
        gradient.setColorAt(1, ramp(SCALE, (b["pressure_m"] - lo) / (hi - lo)))
        return gradient

    def _node_color(self, node, data):
        if node.kind != "junction":
            return QColor("#d9e9e1")
        if data and self.color_metric in NODE_METRIC and data.get("pressure_m") is not None:
            lo, hi = self._range("pressure")
            return ramp(SCALE, (data["pressure_m"] - lo) / (hi - lo))
        return QColor("#4c9b87")

    # -------------------------------------------------------------- painting
    def _label(self, p, rect, text, color="#385d4e"):
        if not self.show_labels:
            return
        metrics = p.fontMetrics()
        w = metrics.horizontalAdvance(text) + 8
        box = QRectF(rect.center().x() - w / 2, rect.top(), w, metrics.height() + 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QColor(color))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def paintEvent(self, event):
        self.reindex()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.CANVAS))
        self._paint_grid(p)
        for l in self.document.links:
            self._paint_link(p, l)
        if self.connection_start and self._mouse is not None:
            start = self._nodes.get(self.connection_start)
            if start is not None:
                pen = QPen(QColor(theme.HIGHLIGHT), 2, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(self.screen((start.x, start.y, start.z)), self._mouse)
        nodes = sorted(self.document.nodes, key=lambda n: self.project_world((n.x, n.y, n.z))[2])
        for n in nodes:
            self._paint_node(p, n)
        self._paint_overlay(p)

    def _paint_grid(self, p):
        if self.dimension == "2d":
            # World-aligned grid that follows pan/zoom (1, 5, 10, 50 … m).
            scale = self.base_scale() * self.zoom
            step = 1.0
            while step * scale < 22:
                step *= 5 if str(step)[0] == "1" else 2
            c = self.center()
            left = (0 - self.width() / 2 - self.pan.x()) / scale + c[0]
            top = -(0 - self.height() / 2 - self.pan.y()) / scale + c[1]
            x0 = math.floor(left / step) * step
            y0 = math.floor(top / step) * step
            p.setPen(QPen(QColor("#e3ebe6"), 1))
            x = x0
            while True:
                sx = self.screen((x, 0, 0)).x()
                if sx > self.width():
                    break
                p.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
                x += step
            y = y0
            while True:
                sy = self.screen((0, y, 0)).y()
                if sy > self.height():
                    break
                p.drawLine(QPointF(0, sy), QPointF(self.width(), sy))
                y -= step
            self._grid_step = step
        else:
            p.setPen(QPen(QColor("#dce6e1"), 1))
            for x in range(12, self.width(), 24):
                for y in range(12, self.height(), 24):
                    p.drawPoint(x, y)

    def _paint_link(self, p, l):
        pts = self.line_points(l)
        if len(pts) < 2:
            return
        data = self.point["links"].get(l.id, {}) if self.point else {}
        color, width = self._link_style(l, data)
        key = ("link", l.id)
        if key in (self.selection, self.hover):
            glow = QPen(QColor(theme.HIGHLIGHT_SOFT if key == self.hover else theme.HIGHLIGHT), width + 7)
            glow.setCapStyle(Qt.PenCapStyle.RoundCap)
            glow.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            glow.setColor(QColor(glow.color().red(), glow.color().green(), glow.color().blue(), 110))
            p.setPen(glow)
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
        if self.selection == key:
            color = QColor(theme.HIGHLIGHT)
        gradient = self._pressure_gradient(l, pts) if l.kind == "pipe" and self.selection != key else None
        pen = QPen(color, width)
        if gradient is not None and data.get("active", True):
            pen = QPen(QBrush(gradient), 4.5)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if not data.get("active", l.enabled):
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        if self.animate and data and data.get("active", True):
            self._paint_particles(p, l, pts, data)
        mid = pts[len(pts) // 2] if len(pts) > 2 else (pts[0] + pts[-1]) / 2
        if l.kind in ("pump", "valve"):
            p.setPen(QPen(color, 2.2))
            p.setBrush(QColor("#fff"))
            p.drawEllipse(mid, 13, 13)
            p.setBrush(color)
            a, b = pts[0], pts[-1]
            angle = math.atan2(b.y() - a.y(), b.x() - a.x())
            d = QPointF(math.cos(angle), math.sin(angle))
            nrm = QPointF(-d.y(), d.x())
            if l.kind == "pump":
                p.drawPolygon(QPolygonF([mid + d * 8, mid - d * 5 + nrm * 7, mid - d * 5 - nrm * 7]))
            else:
                p.drawPolygon(
                    QPolygonF([mid - d * 8 + nrm * 6, mid + d * 8 - nrm * 6,
                               mid + d * 8 + nrm * 6, mid - d * 8 - nrm * 6])
                )
        elif data and abs(data.get("flow_m3h", 0)) > 1e-4:
            a, b = pts[0], pts[-1]
            angle = math.atan2(b.y() - a.y(), b.x() - a.x()) + (
                math.pi if data["flow_m3h"] < 0 else 0
            )
            direction = QPointF(math.cos(angle), math.sin(angle))
            normal = QPointF(-direction.y(), direction.x())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color.darker(115))
            size = 4 + width
            p.drawPolygon(
                QPolygonF(
                    [
                        mid + direction * size,
                        mid - direction * size * 0.7 + normal * size * 0.6,
                        mid - direction * size * 0.7 - normal * size * 0.6,
                    ]
                )
            )
        label = l.id
        metric = LINK_METRIC.get(self.color_metric)
        if data and metric == "flow":
            label += f" · {data.get('flow_m3h', 0):.2f} m³/h"
        elif data and metric == "velocity" and l.kind == "pipe":
            label += f" · {data.get('velocity_ms', 0):.2f} m/s"
        elif data and metric == "headloss" and l.kind == "pipe":
            label += f" · {link_value(l, data, 'headloss'):.1f} m/km"
        self._label(p, QRectF(mid.x() - 90, mid.y() - 34, 180, 18), label, "#4f6f62")
        self._paint_flag(p, key, mid + QPointF(12, -12))

    def _paint_node(self, p, n):
        q = self.screen((n.x, n.y, n.z))
        data = self.point["nodes"].get(n.id, {}) if self.point else {}
        key = ("node", n.id)
        if key in (self.selection, self.hover):
            p.setPen(Qt.PenStyle.NoPen)
            c = QColor(theme.HIGHLIGHT if key == self.selection else theme.HIGHLIGHT_SOFT)
            c.setAlpha(90)
            p.setBrush(c)
            p.drawEllipse(q, 19, 19)
        if self.selection == key:
            p.setPen(QPen(QColor(theme.HIGHLIGHT), 3))
        else:
            p.setPen(QPen(QColor("#30715f"), 1.6))
        p.setBrush(self._node_color(n, data))
        if n.kind == "junction":
            radius = 8 if n.demand_m3h <= 0 else 9.5
            p.drawEllipse(q, radius, radius)
            if n.demand_m3h > 0 and not data:
                p.setBrush(QColor("#fff"))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(q, 3, 3)
        elif n.kind == "reservoir":
            path = QPainterPath()
            path.moveTo(q + QPointF(-14, -10))
            path.lineTo(q + QPointF(14, -10))
            path.lineTo(q + QPointF(10, 11))
            path.lineTo(q + QPointF(-10, 11))
            path.closeSubpath()
            p.drawPath(path)
            p.setPen(QPen(QColor("#3b78b4"), 1.6))
            for dy in (-3, 3):
                wave = QPainterPath(q + QPointF(-9, dy))
                wave.cubicTo(q + QPointF(-5, dy - 3), q + QPointF(-1, dy + 3), q + QPointF(3, dy))
                wave.cubicTo(q + QPointF(5, dy - 2), q + QPointF(7, dy + 2), q + QPointF(9, dy))
                p.drawPath(wave)
        else:
            body = QRectF(q.x() - 11, q.y() - 13, 22, 26)
            p.drawRoundedRect(body, 4, 4)
            level = 0.5
            if data and data.get("tank_level_m") is not None and n.tank_max_m > n.tank_min_m:
                level = (data["tank_level_m"] - n.tank_min_m) / (n.tank_max_m - n.tank_min_m)
            elif n.tank_max_m > n.tank_min_m:
                level = (n.tank_level_m - n.tank_min_m) / (n.tank_max_m - n.tank_min_m)
            level = max(0.0, min(1.0, level))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#6fa6cf"))
            fill = QRectF(body.left() + 2, body.bottom() - 2 - (body.height() - 4) * level,
                          body.width() - 4, (body.height() - 4) * level)
            p.drawRoundedRect(fill, 2, 2)
        if self.connection_start == n.id:
            p.setPen(QPen(QColor(theme.HIGHLIGHT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(q, 16, 16)
        label = n.id
        if data and self.color_metric in NODE_METRIC and data.get("pressure_m") is not None:
            label += f" · {data['pressure_m']:.1f} m"
        self._label(p, QRectF(q.x() - 90, q.y() + 14, 180, 20), label)
        self._paint_flag(p, key, q + QPointF(10, -12))

    def _paint_flag(self, p, key, at):
        severity = self.flags.get(key)
        if not severity:
            return
        color = QColor({"error": "#c8553d", "warning": "#e39b3b"}.get(severity, "#6fa6cf"))
        p.setPen(QPen(QColor("white"), 1.5))
        p.setBrush(color)
        p.drawEllipse(at, 7, 7)
        p.setPen(QColor("white"))
        font = p.font()
        bold = QFont(font)
        bold.setBold(True)
        p.setFont(bold)
        p.drawText(QRectF(at.x() - 7, at.y() - 8, 14, 16), Qt.AlignmentFlag.AlignCenter, "!")
        p.setFont(font)

    def _paint_overlay(self, p):
        p.setPen(QColor("#839b8e"))
        if self.dimension == "2d":
            text = "二维 XY：拖动节点 / 右键或中键平移 / ⌘·Ctrl+滚轮或双指捏合缩放"
            if self.editable:
                text += " / Esc 取消"
        else:
            text = "三维轴测：拖动旋转 / 右键或中键平移 / ⌘·Ctrl+滚轮或双指捏合缩放"
        p.drawText(14, self.height() - 12, text)
        info = f"{len(self.document.nodes)} 节点 · {len(self.document.links)} 连接 · 缩放 {self.zoom * 100:.0f}%"
        if self.dimension == "2d" and getattr(self, "_grid_step", None):
            info += f" · 网格 {self._grid_step:g} m"
        if self.snap and self.editable:
            info += f" · 吸附 {self.snap_step:g} m"
        p.drawText(14, 22, info)
        if self.point:
            top = 12
            if self.color_metric in NODE_METRIC:
                self._paint_legend(p, "pressure", top)
                top += 66
            metric = LINK_METRIC.get(self.color_metric)
            if metric:
                self._paint_legend(p, metric, top)
        if self.dimension == "3d":
            anchor = QPointF(self.width() - 60, self.height() - 60)
            origin = self.project_world((0, 0, 0))
            for label, axis, color in [
                ("X", (1, 0, 0), "#be7065"),
                ("Y", (0, 1, 0), "#6b9b68"),
                ("Z", (0, 0, 1), "#6e8fbd"),
            ]:
                projected = self.project_world(axis)
                end = anchor + QPointF(projected[0] - origin[0], projected[1] - origin[1]) * 32
                p.setPen(QPen(QColor(color), 2))
                p.drawLine(anchor, end)
                p.drawText(end + QPointF(3, -3), label)

    def _paint_legend(self, p, metric, top=12):
        name, unit = METRIC_TEXT.get(metric, (metric, ""))
        lo, hi = self._range(metric)
        stops = SCALE if metric == "pressure" else LINK_STOPS[metric]
        box = QRectF(self.width() - 200, top, 186, 58)
        p.setPen(QPen(QColor("#dbe6df"), 1))
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("#4f6f62"))
        p.drawText(QRectF(box.left() + 10, box.top() + 4, 170, 16), Qt.AlignmentFlag.AlignLeft,
                   f"{name} / {unit}")
        bar = QRectF(box.left() + 10, box.top() + 24, box.width() - 20, 10)
        gradient = QLinearGradient(bar.topLeft(), bar.topRight())
        for i, c in enumerate(stops):
            gradient.setColorAt(i / (len(stops) - 1), QColor(c))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gradient)
        p.drawRoundedRect(bar, 3, 3)
        p.setPen(QColor("#6a8577"))
        p.drawText(QRectF(bar.left(), bar.bottom() + 2, 80, 14), Qt.AlignmentFlag.AlignLeft, f"{lo:.1f}")
        p.drawText(QRectF(bar.right() - 80, bar.bottom() + 2, 80, 14), Qt.AlignmentFlag.AlignRight,
                   f"{hi:.1f}")
        req = self.required_pressure
        if metric == "pressure" and req is not None and lo < req < hi:
            x = bar.left() + (req - lo) / (hi - lo) * bar.width()
            p.setPen(QPen(QColor("#23463a"), 2))
            p.drawLine(QPointF(x, bar.top() - 3), QPointF(x, bar.bottom() + 3))
            p.drawText(QRectF(x - 30, bar.bottom() + 2, 60, 14), Qt.AlignmentFlag.AlignCenter,
                       f"需 {req:g}")

    # -------------------------------------------------------------- animation
    def set_animate(self, value):
        self.animate = bool(value)
        self._tick = time.monotonic()
        if self.animate:
            self._anim.start()
        else:
            self._anim.stop()
        self.update()

    def _advance_animation(self):
        now = time.monotonic()
        self.phase += min(0.1, now - self._tick)
        self._tick = now
        if self.isVisible() and self.point:
            self.update()

    def _paint_particles(self, p, link, pts, data):
        """Dots moving along the link; screen speed grows with the velocity."""
        flow = data.get("flow_m3h") or 0
        if abs(flow) < 1e-4:
            return
        if link.kind == "pipe":
            lo, hi = self._range("velocity")
            t = (abs(data.get("velocity_ms") or 0) - lo) / (hi - lo)
        else:
            lo, hi = self._range("flow")
            t = (abs(flow) - lo) / (hi - lo)
        t = max(0.0, min(1.0, t))
        speed = 18 + 110 * t  # px / s
        spacing = 26.0
        segments = [(a, b, math.hypot(b.x() - a.x(), b.y() - a.y())) for a, b in zip(pts, pts[1:])]
        total = sum(s[2] for s in segments)
        if total < 4:
            return
        offset = (self.phase * speed) % spacing
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 230))
        d = offset
        while d < total:
            along = d if flow > 0 else total - d
            for a, b, length in segments:
                if along <= length or (a, b, length) == segments[-1]:
                    f = along / length if length else 0
                    q = QPointF(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f)
                    p.drawEllipse(q, 2.2, 2.2)
                    break
                along -= length
            d += spacing

    # ---------------------------------------------------------------- tooltip
    def describe(self, hit):
        if not hit:
            return ""
        kind, identifier = hit
        if kind == "node":
            n = self._nodes.get(identifier)
            if n is None:
                return ""
            names = {"junction": "节点", "reservoir": "水源", "tank": "水箱"}
            lines = [f"<b>{n.id}</b> · {names.get(n.kind, n.kind)}",
                     f"高程 {n.elevation_m:g} m · Z {n.z:g} m"]
            if n.kind == "junction" and n.demand_m3h:
                lines.append(f"需水 {n.demand_m3h:g} m³/h")
            if n.kind == "reservoir":
                lines.append(f"总水头 {n.source_head_m:g} m")
            data = self.point["nodes"].get(identifier) if self.point else None
            if data:
                lines.append(f"压力 {data['pressure_m']:.2f} m · 水头 {data['head_m']:.2f} m")
                if data.get("requested_m3h"):
                    lines.append(f"供水 {data['demand_m3h']:.2f} / {data['requested_m3h']:.2f} m³/h")
        else:
            l = next((x for x in self.document.links if x.id == identifier), None)
            if l is None:
                return ""
            names = {"pipe": "管道", "pump": "离心泵", "valve": "调节阀"}
            lines = [f"<b>{l.id}</b> · {names.get(l.kind, l.kind)} · {l.start} → {l.end}"]
            if l.kind == "pipe":
                lines.append(f"L {l.length_m:g} m · D {l.diameter_m * 1000:g} mm")
            elif l.kind == "pump":
                lines.append(f"转速比 {l.speed_ratio:g} · {l.nominal_rpm:g} rpm")
            else:
                lines.append(f"开度 {l.opening:g}")
            if not l.enabled:
                lines.append("（停用）")
            data = self.point["links"].get(identifier) if self.point else None
            if data:
                lines.append(f"流量 {data['flow_m3h']:.2f} m³/h")
                if l.kind == "pipe":
                    lines.append(f"流速 {data['velocity_ms']:.2f} m/s · 损失 {data['headloss_m']:.2f} m")
                if l.kind == "pump" and data.get("active"):
                    lines.append(
                        f"扬程 {data.get('head_m', 0):.2f} m · 电功率 {data.get('power_kw', 0):.2f} kW"
                        f" · 效率 {data.get('efficiency', 0) * 100:.0f}%"
                    )
        return "<br>".join(lines)

    # ------------------------------------------------------------------ input
    def event(self, event):
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.zoom_by(1 + event.value(), event.position())
                return True
            if event.gestureType() == Qt.NativeGestureType.SmartZoomNativeGesture:
                self.fit()
                return True
        return super().event(event)

    def mousePressEvent(self, event):
        self.setFocus()
        self._last = event.position()
        self._moved = False
        if (
            event.button() == Qt.MouseButton.MiddleButton
            or event.button() == Qt.MouseButton.RightButton
        ):
            self._drag = "pan"
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self.hit(event.position())
        if self.dimension == "3d":
            self._drag = "rotate"
            if hit:
                self.selection = hit
                self.selected.emit(*hit)
            self.update()
            return
        if self.editable and self.mode in ("junction", "reservoir", "tank"):
            x, y = self.world_xy(event.position())
            self.add_requested.emit(self.mode, x, y)
            return
        if self.editable and self.mode in ("pipe", "pump", "valve"):
            if hit and hit[0] == "node":
                if self.connection_start and self.connection_start != hit[1]:
                    self.connect_requested.emit(self.mode, self.connection_start, hit[1])
                    self.connection_start = None
                else:
                    self.connection_start = hit[1]
                self.update()
            return
        self.selection = hit
        if hit:
            self.selected.emit(*hit)
            if self.editable and hit[0] == "node":
                self._drag = hit[1]
        else:
            self._drag = "pan"
            self.deselected.emit()
        self.update()

    def mouseMoveEvent(self, event):
        self._mouse = event.position()
        if self.dimension == "2d":
            x, y = self.world_xy(event.position())
            self.cursor_moved.emit(x, y)
        if not self._last or not self._drag:
            hit = self.hit(event.position())
            if hit != self.hover:
                self.hover = hit
                text = self.describe(hit)
                if text:
                    QToolTip.showText(event.globalPosition().toPoint(), text, self)
                else:
                    QToolTip.hideText()
                if self.editable and self.mode == "select":
                    self.setCursor(
                        Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor
                    )
                elif self.editable:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                self.update()
            elif self.connection_start:
                self.update()
            return
        delta = event.position() - self._last
        if self._drag == "pan":
            self.pan += delta
        elif self._drag == "rotate":
            self.yaw += delta.x() * 0.008
            self.pitch = max(-1.5, min(1.5, self.pitch + delta.y() * 0.008))
        else:
            if not self._moved:
                self.begin_edit.emit()
            n = next(n for n in self.document.nodes if n.id == self._drag)
            scale = self.base_scale() * self.zoom
            # Fixed screen-to-world differential avoids a changing bounds centre during drag.
            n.x += delta.x() / scale
            n.y -= delta.y() / scale
        self._moved = True
        self._last = event.position()
        self.update()

    def mouseReleaseEvent(self, event):
        changed = self._drag not in (None, "pan", "rotate") and self._moved
        if changed and self.snap:
            n = self._nodes.get(self._drag)
            if n is not None:
                n.x, n.y = self._snap(n.x), self._snap(n.y)
        if self._drag == "pan":
            self.unsetCursor()
        self._drag = None
        self._last = None
        if changed:
            self.changed.emit()

    def leaveEvent(self, event):
        self._mouse = None
        if self.hover:
            self.hover = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        if not zoom_wheel(self, event):
            return
        self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15, event.position())

    def resizeEvent(self, event):
        self._view_scale = None
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.connection_start = None
            self.update()
            if self.mode != "select":
                self.mode = "select"
                self.changed_mode()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.editable:
            self.delete_requested.emit()
        elif event.key() == Qt.Key.Key_F:
            self.fit()
        else:
            super().keyPressEvent(event)

    def changed_mode(self):
        """Hook overwritten by the window so palette buttons follow Esc."""


class TrendWidget(QWidget):
    """Step-line time series, an optional element series and the pump Q–H view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.key = "power_kw"
        self.label = "电功率 / kW"
        self.cursor = 0
        self.network = None
        self.pump_id = None
        self.series = None
        self.reference = None
        self.profile = None
        self._hover = None
        self.setMinimumHeight(170)
        self.setMouseTracking(True)

    def values(self):
        if self.series is not None:
            return self.series
        return [float(x.get(self.key) or 0) for x in self.points]

    def _rect(self):
        return QRectF(62, 30, max(10, self.width() - 86), max(10, self.height() - 62))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        if self.key == "pump_curve":
            self.paint_curve(p)
            return
        if self.key == "profile":
            self.paint_profile(p)
            return
        r = self._rect()
        p.setPen(QColor("#4f6f62"))
        title = QFont(p.font())
        title.setBold(True)
        p.setFont(title)
        p.drawText(14, 19, self.label)
        p.setFont(QFont(self.font()))
        values = self.values()
        if not values or not self.points:
            p.setPen(QColor("#9ab0a3"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "计算后显示趋势")
            return
        low = min(0, min(values))
        high = max(values)
        if self.reference is not None:
            low = min(low, self.reference)
            high = max(high, self.reference)
        high = high if high > low else low + 1
        high += (high - low) * 0.1
        last = max(0.001, self.points[-1]["hour"])
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor("#edf2ee"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#89a090"))
            p.drawText(
                QRectF(0, y - 8, 56, 16),
                Qt.AlignmentFlag.AlignRight,
                f"{low + (high - low) * i / 4:.2f}",
            )

        def xy(i):
            return QPointF(
                r.left() + self.points[i]["hour"] / last * r.width(),
                r.bottom() - (values[i] - low) / (high - low) * r.height(),
            )

        if self.reference is not None:
            y = r.bottom() - (self.reference - low) / (high - low) * r.height()
            pen = QPen(QColor("#c8553d"), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.drawText(QPointF(r.right() - 120, y - 4), f"要求 {self.reference:g}")
        area = QPainterPath(QPointF(xy(0).x(), r.bottom()))
        line = QPainterPath(xy(0))
        area.lineTo(xy(0))
        for i in range(1, len(values)):
            a, b = xy(i - 1), xy(i)
            for path in (area, line):
                path.lineTo(QPointF(b.x(), a.y()))
                path.lineTo(b)
        area.lineTo(QPointF(xy(len(values) - 1).x(), r.bottom()))
        area.closeSubpath()
        fill = QColor(theme.ACCENT)
        fill.setAlpha(28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(area)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        p.drawPath(line)
        p.setPen(QColor("#8fa18f"))
        p.drawText(int(r.left()), self.height() - 10, "0 h")
        p.drawText(QRectF(r.right() - 100, self.height() - 24, 100, 16), Qt.AlignmentFlag.AlignRight,
                   f"{last:.2f} h")
        if 0 <= self.cursor < len(self.points):
            q = xy(self.cursor)
            pen = QPen(QColor(theme.HIGHLIGHT), 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(q.x(), r.top()), QPointF(q.x(), r.bottom()))
            p.setBrush(QColor(theme.HIGHLIGHT))
            p.setPen(QPen(QColor("white"), 1.5))
            p.drawEllipse(q, 4, 4)
        if self._hover is not None and 0 <= self._hover < len(values):
            q = xy(self._hover)
            text = f"{self.points[self._hover]['hour']:.2f} h · {values[self._hover]:.3f}"
            w = p.fontMetrics().horizontalAdvance(text) + 12
            box = QRectF(min(q.x() + 8, r.right() - w), max(r.top(), q.y() - 26), w, 20)
            p.setPen(QPen(QColor("#cfdcd3"), 1))
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("#2e5546"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def paint_profile(self, p):
        """Ground elevation, hydraulic grade line and pressure along a path."""
        r = self._rect().adjusted(0, 22, -10, -10)
        title = QFont(p.font())
        title.setBold(True)
        p.setFont(title)
        p.setPen(QColor("#4f6f62"))
        data = self.profile
        p.drawText(14, 19, data["title"] if data else "沿程水力剖面：在结果图中点击一个节点")
        p.setFont(QFont(self.font()))
        if not data or len(data["stations"]) < 2:
            p.setPen(QColor("#9ab0a3"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, data.get("message", "") if data else
                       "选择节点后显示从水源到该节点的测压管水头线（HGL）")
            return
        st = data["stations"]
        required = data.get("required")
        xs = [s["distance"] for s in st]
        values = [s["elevation"] for s in st] + [s["head"] for s in st]
        if required is not None:
            values += [s["elevation"] + required for s in st if s.get("demand")]
        low, high = min(values), max(values)
        pad = max(0.5, (high - low) * 0.08)
        low, high = low - pad, high + pad
        span = max(1e-9, xs[-1] - xs[0])

        def xy(x, y):
            return QPointF(r.left() + (x - xs[0]) / span * r.width(),
                           r.bottom() - (y - low) / (high - low) * r.height())

        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor("#edf2ee"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#89a090"))
            p.drawText(QRectF(0, y - 8, 56, 16), Qt.AlignmentFlag.AlignRight,
                       f"{low + (high - low) * i / 4:.1f}")
        ground = QPainterPath(QPointF(r.left(), r.bottom()))
        for s in st:
            ground.lineTo(xy(s["distance"], s["elevation"]))
        ground.lineTo(QPointF(r.right(), r.bottom()))
        ground.closeSubpath()
        p.setPen(QPen(QColor("#b49a7a"), 1.5))
        p.setBrush(QColor(196, 172, 140, 70))
        p.drawPath(ground)
        # pressure band between ground and HGL
        band = QPainterPath(xy(st[0]["distance"], st[0]["elevation"]))
        for s in st:
            band.lineTo(xy(s["distance"], s["head"]))
        for s in reversed(st):
            band.lineTo(xy(s["distance"], s["elevation"]))
        band.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        fill = QColor("#6fa6cf")
        fill.setAlpha(55)
        p.setBrush(fill)
        p.drawPath(band)
        if required is not None:
            pen = QPen(QColor("#c8553d"), 1.2, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath(xy(st[0]["distance"], st[0]["elevation"] + required))
            for s in st[1:]:
                path.lineTo(xy(s["distance"], s["elevation"] + required))
            p.drawPath(path)
        p.setPen(QPen(QColor(theme.ACCENT), 2.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        hgl = QPainterPath(xy(st[0]["distance"], st[0]["head"]))
        for s in st[1:]:
            hgl.lineTo(xy(s["distance"], s["head"]))
        p.drawPath(hgl)
        for s in st:
            q = xy(s["distance"], s["head"])
            g = xy(s["distance"], s["elevation"])
            p.setPen(QPen(QColor("#6fa6cf"), 1, Qt.PenStyle.DotLine))
            p.drawLine(q, g)
            p.setPen(QPen(QColor("white"), 1.5))
            p.setBrush(QColor(theme.HIGHLIGHT if s.get("pump") else theme.ACCENT))
            p.drawEllipse(q, 4, 4)
            p.setPen(QColor("#2e5546"))
            text = s["id"] if s.get("pressure") is None else f"{s['id']} {s['pressure']:.1f} m"
            w = p.fontMetrics().horizontalAdvance(text) + 6
            left = max(r.left() - 50, min(q.x() - w / 2, r.right() + 8 - w))
            above = q.y() - 20 if q.y() - 20 > r.top() - 18 else q.y() + 6
            p.drawText(QRectF(left, above, w, 16), Qt.AlignmentFlag.AlignCenter, text)
        p.setPen(QColor("#8fa18f"))
        p.drawText(int(r.left()), int(r.bottom() + 18), f"{xs[0]:.0f} m")
        p.drawText(QRectF(r.right() - 160, r.bottom() + 6, 160, 16), Qt.AlignmentFlag.AlignRight,
                   f"沿程距离 {xs[-1]:.0f} m")
        legend = [("测压管水头 HGL", theme.ACCENT), ("地面 / 高程", "#b49a7a"),
                  ("压力（蓝色区域）", "#6fa6cf")]
        if required is not None:
            legend.append(("所需压力线", "#c8553d"))
        x = r.left() + 80
        y = r.bottom() + 8
        for text, color in legend:
            w = p.fontMetrics().horizontalAdvance(text) + 22
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRect(QRectF(x, y + 3, 10, 10))
            p.setPen(QColor("#4f6f62"))
            p.drawText(QPointF(x + 14, y + 12), text)
            x += w

    def mouseMoveEvent(self, event):
        if not self.points or self.key in ("pump_curve", "profile"):
            return
        r = self._rect()
        last = max(0.001, self.points[-1]["hour"])
        hour = (event.position().x() - r.left()) / r.width() * last
        index = min(range(len(self.points)), key=lambda i: abs(self.points[i]["hour"] - hour))
        if index != self._hover:
            self._hover = index
            self.update()

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def paint_curve(self, p):
        p.setPen(QColor("#4f6f62"))
        title = "所选泵 Q–H 曲线 · 点击结果图中的泵切换"
        if self.network and self.points and self.pump_id:
            rpm = self.points[self.cursor]["links"].get(self.pump_id, {}).get("rpm", 0)
            title = f"{self.pump_id} · {rpm:.0f} rpm · Q–H 曲线（点击结果图中的泵切换）"
        p.drawText(14, 19, title)
        if not self.network or not self.points:
            return
        pump = next(
            (l for l in self.network.links if l.kind == "pump" and l.id == self.pump_id), None
        )
        if pump is None:
            p.drawText(30, 65, "当前结果没有泵")
            return
        state = self.points[self.cursor]["links"][pump.id]
        speed = state.get("rpm", 0) / pump.nominal_rpm if state.get("active") else pump.speed_ratio
        draw_pump_curve(p, self._rect(), pump, speed, state if state.get("active") else None)


def draw_pump_curve(p, r, pump, speed=1.0, state=None, curve=None):
    """Draw H(Q) and η(Q) of a pump curve scaled by the affinity laws."""
    from backend.models import G

    curve = curve if curve is not None else [
        (c.flow_m3h, c.head_m, c.shaft_kw) for c in pump.curve
    ]
    if len(curve) < 2:
        return
    s = max(speed, 1e-6)
    values = [(q * s, h * s * s, kw * s**3) for q, h, kw in curve]
    maxq = max(q for q, _, _ in values) * 1.1 or 1
    maxh = max(h for _, h, _ in values) * 1.1 or 1

    def xy(q, h):
        return QPointF(r.left() + q / maxq * r.width(), r.bottom() - h / maxh * r.height())

    for i in range(5):
        y = r.bottom() - r.height() * i / 4
        p.setPen(QPen(QColor("#edf2ee"), 1))
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QColor("#89a090"))
        p.drawText(QRectF(r.left() - 58, y - 8, 54, 16), Qt.AlignmentFlag.AlignRight,
                   f"{maxh * i / 4:.1f} m")
    eta = [
        (q, 998.2 * G * q / 3600 * h / (kw * 1000) if kw > 0 else 0) for q, h, kw in values
    ]
    p.setPen(QPen(QColor("#e39b3b"), 1.6, Qt.PenStyle.DashLine))
    path = QPainterPath(QPointF(r.left() + eta[0][0] / maxq * r.width(), r.bottom() - eta[0][1] * r.height()))
    for q, e in eta[1:]:
        path.lineTo(QPointF(r.left() + q / maxq * r.width(), r.bottom() - e * r.height()))
    p.drawPath(path)
    best = max(eta, key=lambda x: x[1])
    p.drawText(QPointF(r.right() - 120, r.top() + 14), f"η 最高 {best[1] * 100:.0f}% @ {best[0]:.1f}")
    p.setPen(QPen(QColor(theme.ACCENT), 2.2))
    path = QPainterPath(xy(values[0][0], values[0][1]))
    for q, h, _ in values[1:]:
        path.lineTo(xy(q, h))
    p.drawPath(path)
    p.setBrush(QColor(theme.ACCENT))
    for q, h, _ in values:
        p.drawEllipse(xy(q, h), 2.5, 2.5)
    p.setPen(QColor("#8fa18f"))
    p.drawText(int(r.left()), int(r.bottom() + 22), "0 m³/h")
    p.drawText(QRectF(r.right() - 110, r.bottom() + 8, 110, 16), Qt.AlignmentFlag.AlignRight,
               f"{maxq:.1f} m³/h")
    if state:
        point = xy(state["flow_m3h"], state["head_m"])
        p.setBrush(QColor(theme.HIGHLIGHT))
        p.setPen(QPen(QColor("white"), 2))
        p.drawEllipse(point, 6, 6)
        p.setPen(QColor("#2e5546"))
        p.drawText(point + QPointF(10, -8), f"{state['flow_m3h']:.2f} m³/h, {state['head_m']:.2f} m")
