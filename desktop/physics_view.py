"""Portable software-rendered views of actual CFD arrays, suitable for offscreen QA."""

import math
import numpy as np
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget
from .scrolling import zoom_wheel


def color(f):
    f = max(0, min(1, float(f)))
    return QColor.fromHsvF((1 - f) * 0.66, 0.85, 0.95)


class FieldView(QWidget):
    def __init__(self):
        super().__init__()
        self.data = None
        self.yaw = -0.6
        self.pitch = 0.55
        self.zoom = 1
        self.last = None
        self.vectors = True
        self.placeholder = "加载本机 CFD 结果，显示求解器输出的真实网格场"
        self.setMinimumSize(380, 230)

    def set_data(self, data):
        self.data = data
        self.update()

    def mousePressEvent(self, e):
        self.last = e.position()

    def mouseMoveEvent(self, e):
        if self.last is None:
            return
        delta = e.position() - self.last
        self.yaw += delta.x() * 0.008
        self.pitch = max(-1.5, min(1.5, self.pitch + delta.y() * 0.008))
        self.last = e.position()
        self.update()

    def mouseReleaseEvent(self, e):
        self.last = None

    def wheelEvent(self, e):
        if not zoom_wheel(self, e):
            return
        self.zoom = max(0.2, min(10, self.zoom * math.exp(e.angleDelta().y() / 1200)))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#101b2b"))
        p.setPen(QColor("#b9cddd"))
        if self.data is None:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            return
        d = self.data
        xyz = d["points"]
        shell = d["surface_points"]
        lo = shell.min(axis=0)
        hi = shell.max(axis=0)
        center = (lo + hi) / 2
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        a, b = math.cos(self.pitch), math.sin(self.pitch)
        matrix = np.array([[c, -s, 0], [s * b, c * b, -a], [s * a, c * a, b]])
        span = max(np.linalg.norm(hi - lo), 1e-12)
        scale = min(self.width() - 100, self.height() - 100) / span * self.zoom

        def project(points):
            r = (points - center) @ matrix.T
            return np.column_stack(
                (r[:, 0] * scale + self.width() / 2, r[:, 1] * scale + self.height() / 2, r[:, 2])
            )

        sp = project(shell)
        pp = project(xyz)
        p.setPen(QPen(QColor(105, 148, 176, 45), 0.5))
        p.setBrush(QColor(57, 83, 105, 12))
        for ids in d["surface_triangles"]:
            poly = QPolygonF([QPointF(*sp[i, :2]) for i in ids])
            p.drawPolygon(poly)
        vmin = d.get("color_minimum", d["minimum"])
        vmax = d.get("color_maximum", d["maximum"])
        den = max(vmax - vmin, 1e-12)
        if len(d.get("colored_triangles", [])):
            cp = d["colored_points"]
            ids = d["colored_triangles"]
            proj = project(cp)
            normals = np.cross(cp[ids[:, 1]] - cp[ids[:, 0]], cp[ids[:, 2]] - cp[ids[:, 0]])
            normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-20)
            lighting = 0.48 + 0.52 * np.abs((normals @ matrix.T) @ np.array([0.3, -0.4, 0.866]))
            face_values = d["colored_values"][ids].mean(axis=1)
            for i in np.argsort(proj[ids, 2].mean(axis=1)):
                shade = (
                    QColor("#ffb74c")
                    if d.get("vapor_isosurface")
                    else color((face_values[i] - vmin) / den)
                )
                shade = QColor.fromRgbF(
                    shade.redF() * lighting[i],
                    shade.greenF() * lighting[i],
                    shade.blueF() * lighting[i],
                    0.96 if d.get("vapor_isosurface") else 0.9,
                )
                p.setBrush(shade)
                p.setPen(QPen(QColor(230, 245, 250, 28), 0.35))
                p.drawPolygon(QPolygonF([QPointF(*proj[j, :2]) for j in ids[i]]))
        if d.get("is_slice") or not len(d.get("colored_triangles", [])):
            for i in np.argsort(pp[:, 2]):
                if d["metric"] == "vapour_fraction" and d["values"][i] < 0.01:
                    continue
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color((d["values"][i] - vmin) / den))
                p.drawEllipse(QPointF(*pp[i, :2]), 2.1, 2.1)
        if self.vectors and d["metric"] != "vapour_fraction":
            velocity = d["velocity"] @ matrix.T
            maxv = max(np.linalg.norm(velocity, axis=1).max(), 1e-12)
            p.setPen(QPen(QColor(230, 242, 250, 170), 0.7))
            for i in range(0, len(xyz), max(1, len(xyz) // 110)):
                start = pp[i, :2]
                end = start + velocity[i, :2] / maxv * 25
                p.drawLine(QPointF(*start), QPointF(*end))
                delta = end - start
                length = np.linalg.norm(delta)
                if length > 2:
                    tangent = delta / length
                    normal = np.array([-tangent[1], tangent[0]])
                    for sign in (-1, 1):
                        p.drawLine(QPointF(*end), QPointF(*(end - 4 * tangent + sign * 2 * normal)))
        p.setPen(QPen(QColor("#f8df8c"), 2.2))
        for line in d["streamlines"]:
            projected = project(line)
            p.drawPolyline(QPolygonF([QPointF(*row[:2]) for row in projected]))
        if d.get("vapor_isosurface"):
            p.fillRect(self.width() - 150, 52, 16, 16, QColor("#ffb74c"))
            p.setPen(QColor("#ffd99e"))
            p.drawText(self.width() - 125, 65, "αv = 0.1")
        else:
            for j in range(150):
                p.fillRect(self.width() - 45, 55 + j, 15, 2, color(1 - j / 149))
        p.setPen(QColor("#d3e5ef"))
        p.drawText(16, 24, f"t = {d['seconds']:.6g} s   {d['cell_count']:,} cells   {d['metric']}")
        p.drawText(
            16,
            self.height() - 17,
            f"范围 {d['minimum']:.5g} — {d['maximum']:.5g} | "
            + (
                "汽相率等值面 α=0.1"
                if d.get("vapor_isosurface")
                else "真实网格场 · Ctrl或⌘+滚轮缩放"
            ),
        )


class SeriesView(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.label = ""
        self.setMinimumHeight(160)

    def set_series(self, x, y, label):
        self.rows = list(zip(x, y))
        self.label = label
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#101b2b"))
        p.setPen(QColor("#c3d7e5"))
        p.drawText(15, 24, self.label)
        if len(self.rows) < 2:
            p.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "选择结果与物理量后显示时序 / 频谱"
            )
            return
        a = np.asarray(self.rows)
        xmin, ymin = a.min(axis=0)
        xmax, ymax = a.max(axis=0)
        dy = max(ymax - ymin, abs(ymax) * 1e-6, 1e-9)
        dx = max(xmax - xmin, 1e-12)
        r = QRectF(75, 45, max(20, self.width() - 105), max(20, self.height() - 100))
        p.setPen(QPen(QColor("#31485c"), 1))
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#b7cbd8"))
            p.drawText(3, int(y + 4), f"{ymin + dy * i / 4:.4g}")
            p.setPen(QColor("#31485c"))
        # Min/max bins preserve short impact peaks when screen resolution is lower than sampling.
        stride = max(1, len(a) // max(2, int(r.width())))
        points = []
        for j in range(0, len(a), stride):
            block = a[j : j + stride]
            ids = sorted(
                set([0, len(block) - 1, int(np.argmin(block[:, 1])), int(np.argmax(block[:, 1]))])
            )
            points.extend(block[ids])
        poly = QPolygonF(
            [
                QPointF(
                    r.left() + (x - xmin) / dx * r.width(),
                    r.bottom() - (y - ymin) / dy * r.height(),
                )
                for x, y in points
            ]
        )
        p.setPen(QPen(QColor("#45d8be"), 1.3))
        p.drawPolyline(poly)
        p.setPen(QColor("#b7cbd8"))
        p.drawText(int(r.left()), self.height() - 20, f"{xmin:.5g}")
        p.drawText(int(r.right() - 85), self.height() - 20, f"{xmax:.5g}")
