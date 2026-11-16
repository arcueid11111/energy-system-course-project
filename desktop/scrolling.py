"""Scroll oversized forms without forcing their containing window off screen."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QFrame, QLayout, QScrollArea


def scrollable(widget, name):
    layout = widget.layout()
    if layout is not None:
        # Recalculate the scroll range when fields or labels change dynamically.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
    area = QScrollArea()
    area.setObjectName(name)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setWidget(widget)
    return area


def fit_to_screen(widget, width, height):
    screen = widget.screen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, max(240, available.width() - 40))
        height = min(height, max(200, available.height() - 80))
    widget.resize(width, height)


def zoom_wheel(widget, event):
    if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
        event.accept()
        return True
    # QWidget canvases do not reliably bubble wheel events through splitters.
    # Forward both mouse-wheel and trackpad deltas to their enclosing page.
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea):
            forwarded = QWheelEvent(
                parent.viewport().mapFromGlobal(event.globalPosition().toPoint()).toPointF(),
                event.globalPosition(),
                event.pixelDelta(),
                event.angleDelta(),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                event.inverted(),
            )
            QApplication.sendEvent(parent.viewport(), forwarded)
            event.setAccepted(forwarded.isAccepted())
            return False
        parent = parent.parentWidget()
    event.ignore()
    return False
