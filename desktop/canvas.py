import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


class NetworkCanvas(QWidget):
    selected = Signal(str, str)
    add_requested = Signal(str, float, float)
    connect_requested = Signal(str, str, str)
    begin_edit = Signal()
    changed = Signal()
    delete_requested = Signal()

    def __init__(self, document, editable=True, parent=None):
        super().__init__(parent)
        self.document = document
        self.editable = editable
        self.mode = "select"
        self.dimension = "2d"
        self.selection = None
        self.connection_start = None
        self.point = None
        self.color_metric = "pressure"
        self.zoom = 1.0
        self.pan = QPointF()
        self.yaw = math.radians(35)
        self.pitch = math.radians(30)
        self._drag = None
        self._last = None
        self._moved = False
        self.snap = False
        self._view_center = None
        self._view_scale = None
        self.reindex()
        self.setMinimumSize(460, 320)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def set_document(self, document):
        self.document = document
        self.selection = None
        self.connection_start = None
        self.point = None
        self.fit()
        self.update()

    def fit(self):
        self.zoom = 1.0
        self.pan = QPointF()
        self._view_center = None
        self._view_scale = None
        self.reindex()
        self.update()

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
            max(80, self.width() - 120) / max(15, max(xs) - min(xs)),
            max(80, self.height() - 100) / max(15, max(ys) - min(ys)),
        )
        return self._view_scale

    def screen(self, p):
        x, y, _ = self.project_world(p)
        scale = self.base_scale() * self.zoom
        return QPointF(
            self.width() / 2 + self.pan.x() + x * scale,
            self.height() / 2 + self.pan.y() + y * scale,
        )

    def world_xy(self, p):
        c = self.center()
        scale = self.base_scale() * self.zoom
        x = (p.x() - self.width() / 2 - self.pan.x()) / scale + c[0]
        y = -(p.y() - self.height() / 2 - self.pan.y()) / scale + c[1]
        return (round(x) if self.snap else x, round(y) if self.snap else y)

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

    def paintEvent(self, event):
        self.reindex()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#f8fbfa"))
        p.setPen(QPen(QColor("#dce6e1"), 1))
        for x in range(12, self.width(), 24):
            for y in range(12, self.height(), 24):
                p.drawPoint(x, y)
        for l in self.document.links:
            pts = self.line_points(l)
            if len(pts) < 2:
                continue
            data = self.point["links"].get(l.id, {}) if self.point else {}
            color = QColor("#247b6d" if l.enabled else "#b6c5bf")
            if self.selection == ("link", l.id):
                color = QColor("#ce974c")
            pen = QPen(color, 3 if l.kind == "pipe" else 4)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            if not data.get("active", l.enabled):
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            mid = pts[len(pts) // 2] if len(pts) > 2 else (pts[0] + pts[-1]) / 2
            if l.kind in ("pump", "valve"):
                p.setBrush(QColor("#fff"))
                p.drawEllipse(mid, 13, 13)
                p.setBrush(color)
                if l.kind == "pump":
                    p.drawPolygon(
                        QPolygonF(
                            [mid + QPointF(-5, -7), mid + QPointF(8, 0), mid + QPointF(-5, 7)]
                        )
                    )
                else:
                    p.drawPolygon(
                        QPolygonF(
                            [
                                mid + QPointF(-8, -6),
                                mid + QPointF(8, 6),
                                mid + QPointF(8, -6),
                                mid + QPointF(-8, 6),
                            ]
                        )
                    )
            elif data and abs(data.get("flow_m3h", 0)) > 1e-4:
                a, b = pts[0], pts[-1]
                angle = math.atan2(b.y() - a.y(), b.x() - a.x()) + (
                    math.pi if data["flow_m3h"] < 0 else 0
                )
                direction = QPointF(math.cos(angle), math.sin(angle))
                normal = QPointF(-direction.y(), direction.x())
                p.setBrush(color)
                p.drawPolygon(
                    QPolygonF(
                        [
                            mid + direction * 6,
                            mid - direction * 5 + normal * 4,
                            mid - direction * 5 - normal * 4,
                        ]
                    )
                )
            p.setPen(QColor("#648679"))
            label = l.id + (
                f" · {data['flow_m3h']:.2f} m³/h" if data and self.color_metric == "flow" else ""
            )
            p.drawText(
                QRectF(mid.x() - 90, mid.y() - 30, 180, 18), Qt.AlignmentFlag.AlignCenter, label
            )
        nodes = sorted(self.document.nodes, key=lambda n: self.project_world((n.x, n.y, n.z))[2])
        for n in nodes:
            q = self.screen((n.x, n.y, n.z))
            data = self.point["nodes"].get(n.id, {}) if self.point else {}
            color = QColor("#4c9b87")
            if data and self.color_metric == "pressure" and n.kind == "junction":
                color = QColor.fromHsvF(max(0, min(0.36, data["pressure_m"] / 80)), 0.42, 0.87)
            if self.selection == ("node", n.id):
                p.setPen(QPen(QColor("#d59b4d"), 3))
            else:
                p.setPen(QPen(QColor("#30715f"), 1.6))
            p.setBrush(color if n.kind == "junction" else QColor("#d9e9e1"))
            if n.kind == "junction":
                p.drawEllipse(q, 8, 8)
            else:
                p.drawRoundedRect(QRectF(q.x() - 12, q.y() - 13, 24, 26), 3, 3)
                p.drawLine(q + QPointF(-9, 2), q + QPointF(9, 2))
            if self.connection_start == n.id:
                p.setPen(QPen(QColor("#d59b4d"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(q, 16, 16)
            p.setPen(QColor("#385d4e"))
            label = n.id
            if data and self.color_metric == "pressure":
                label += f" · {data['pressure_m']:.1f} m"
            p.drawText(QRectF(q.x() - 90, q.y() + 13, 180, 20), Qt.AlignmentFlag.AlignCenter, label)
        p.setPen(QColor("#839b8e"))
        text = (
            "二维 XY：拖动节点 / 中键平移 / 滚轮缩放"
            if self.dimension == "2d"
            else "三维轴测：拖动旋转 / 中键平移 / 滚轮缩放；高程保留"
        )
        p.drawText(14, self.height() - 14, text)
        p.drawText(
            14,
            23,
            f"{len(self.document.nodes)} 节点 · {len(self.document.links)} 连接 · 坐标单位 m",
        )
        if self.dimension == "3d":
            anchor = QPointF(self.width() - 72, self.height() - 70)
            origin = self.project_world((0, 0, 0))
            for label, axis, color in [
                ("X", (1, 0, 0), "#be7065"),
                ("Y", (0, 1, 0), "#6b9b68"),
                ("Z", (0, 0, 1), "#6e8fbd"),
            ]:
                projected = self.project_world(axis)
                end = anchor + QPointF(projected[0] - origin[0], projected[1] - origin[1]) * 35
                p.setPen(QPen(QColor(color), 2))
                p.drawLine(anchor, end)
                p.drawText(end + QPointF(3, -3), label)

    def mousePressEvent(self, event):
        self.setFocus()
        self._last = event.position()
        self._moved = False
        if (
            event.button() == Qt.MouseButton.MiddleButton
            or event.button() == Qt.MouseButton.RightButton
        ):
            self._drag = "pan"
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
        self.update()

    def mouseMoveEvent(self, event):
        if not self._last or not self._drag:
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
        self._drag = None
        self._last = None
        if changed:
            self.changed.emit()

    def wheelEvent(self, event):
        self.zoom = max(
            0.05, min(40, self.zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15))
        )
        self.update()

    def resizeEvent(self, event):
        self._view_scale = None
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.connection_start = None
            self.mode = "select"
            self.update()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.editable:
            self.delete_requested.emit()
        else:
            super().keyPressEvent(event)


class TrendWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.key = "power_kw"
        self.label = "电功率 / kW"
        self.cursor = 0
        self.network = None
        self.pump_id = None
        self.setMinimumHeight(190)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        if self.key == "pump_curve":
            self.paint_curve(p)
            return
        r = QRectF(55, 28, max(10, self.width() - 80), max(10, self.height() - 65))
        p.setPen(QColor("#688577"))
        p.drawText(14, 17, self.label)
        values = [float(x[self.key] or 0) for x in self.points]
        if not values:
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "计算后显示趋势")
            return
        low = min(0, min(values))
        high = max(values)
        high = high if high > low else low + 1
        high += (high - low) * 0.1
        last = max(0.001, self.points[-1]["hour"])
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor("#e7eee8"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#89a090"))
            p.drawText(
                QRectF(0, y - 8, 48, 16),
                Qt.AlignmentFlag.AlignRight,
                f"{low + (high - low) * i / 4:.2f}",
            )

        def xy(i):
            return QPointF(
                r.left() + self.points[i]["hour"] / last * r.width(),
                r.bottom() - (values[i] - low) / (high - low) * r.height(),
            )

        p.setPen(QPen(QColor("#268572"), 2))
        path = QPainterPath(xy(0))
        for i in range(1, len(values)):
            a, b = xy(i - 1), xy(i)
            path.lineTo(QPointF(b.x(), a.y()))
            path.lineTo(b)
        p.drawPath(path)
        p.setPen(QColor("#8fa18f"))
        p.drawText(int(r.left()), self.height() - 10, "0 h")
        p.drawText(int(r.right() - 60), self.height() - 10, f"{last:.2f} h")
        if 0 <= self.cursor < len(self.points):
            q = xy(self.cursor)
            pen = QPen(QColor("#d5a05b"), 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(q.x(), r.top()), QPointF(q.x(), r.bottom()))

    def paint_curve(self, p):
        p.setPen(QColor("#688577"))
        p.drawText(14, 17, "所选泵 Q–H 曲线 · 点击结果图中的泵切换")
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
        values = [(point.flow_m3h * speed, point.head_m * speed**2) for point in pump.curve]
        maxq = max(q for q, h in values) * 1.1
        maxh = max(h for q, h in values) * 1.1
        r = QRectF(55, 30, max(10, self.width() - 90), max(10, self.height() - 65))

        def xy(q, h):
            return QPointF(r.left() + q / maxq * r.width(), r.bottom() - h / maxh * r.height())

        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor("#e7eee8"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#89a090"))
            p.drawText(2, int(y), f"{maxh * i / 4:.1f}m")
        p.setPen(QPen(QColor("#268572"), 2))
        path = QPainterPath(xy(*values[0]))
        for value in values[1:]:
            path.lineTo(xy(*value))
        p.drawPath(path)
        p.setPen(QColor("#8fa18f"))
        p.drawText(int(r.left()), self.height() - 10, "0 m³/h")
        p.drawText(int(r.right() - 100), self.height() - 10, f"{maxq:.1f} m³/h")
        if state.get("active"):
            point = xy(state["flow_m3h"], state["head_m"])
            p.setBrush(QColor("#d5a05b"))
            p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(point, 5, 5)
        p.setPen(QColor("#688577"))
        p.drawText(int(r.left() + 20), 47, f"{pump.id} · {state.get('rpm', 0):.0f} rpm")
