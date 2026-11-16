"""A fixed-layout result viewer that keeps the principal views on screen."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QSplitter,
    QSizePolicy,
)
from .scrolling import fit_to_screen


class ResultFocus(QDialog):
    """Temporarily enlarge existing application widgets, restoring them on close."""

    def __init__(self, parent, title, subtitle=""):
        super().__init__(parent)
        self.setWindowTitle("FLOWSTATE · " + title)
        self.resize(1380, 720)
        self.borrowed = []
        self.splitter_sizes = {}
        self.setStyleSheet("""
            QDialog{background:#eef4f1;color:#193e36;}
            QLabel{color:#46675d;font-size:14px;}
            QLabel#focusTitle{font-size:25px;font-weight:600;color:#173f34;}
            QLabel#focusSubtitle{font-size:14px;color:#658075;}
            QWidget#focusCard{background:white;border:1px solid #d9e6df;border-radius:8px;}
            QLabel#focusValue{font-size:27px;font-weight:600;color:#16785d;}
            QLabel#focusCaption{font-size:13px;color:#688176;}
            QPushButton{background:white;color:#24624d;border:1px solid #cddfd4;
                border-radius:5px;padding:6px 13px;font-size:14px;}
            QComboBox{font-size:14px;padding:4px;}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)
        top = QHBoxLayout()
        labels = QVBoxLayout()
        self.title = QLabel(title)
        self.title.setObjectName("focusTitle")
        labels.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("focusSubtitle")
        labels.addWidget(self.subtitle)
        top.addLayout(labels, 1)
        close = QPushButton("返回工作台")
        close.clicked.connect(self.close)
        top.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(top)
        self.cards = QHBoxLayout()
        root.addLayout(self.cards)
        self.body = QHBoxLayout()
        self.body.setSpacing(12)
        root.addLayout(self.body, 1)
        self.controls = QHBoxLayout()
        root.addLayout(self.controls)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setMaximumHeight(42)
        root.addWidget(self.note)

    def set_cards(self, entries):
        while self.cards.count():
            self.cards.takeAt(0).widget().deleteLater()
        for caption, value, detail in entries:
            card = QWidget()
            card.setObjectName("focusCard")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(13, 8, 13, 8)
            layout.setSpacing(1)
            for text, name in [
                (caption, "focusCaption"),
                (value, "focusValue"),
                (detail, "focusCaption"),
            ]:
                label = QLabel(str(text))
                label.setObjectName(name)
                layout.addWidget(label)
            self.cards.addWidget(card, 1)

    def add_view(self, widget, stretch=1, borrow=False):
        if borrow:
            parent = widget.parentWidget()
            layout = parent.layout() if parent else None
            index = layout.indexOf(widget) if layout else -1
            original_stretch = layout.stretch(index) if layout and hasattr(layout, "stretch") else 0
            if isinstance(parent, QSplitter):
                self.splitter_sizes.setdefault(parent, parent.sizes())
                index = parent.indexOf(widget)
            self.borrowed.append(
                (
                    widget,
                    parent,
                    layout,
                    index,
                    original_stretch,
                    widget.isHidden(),
                    widget.minimumWidth(),
                    widget.sizePolicy(),
                )
            )
        # Paint-only widgets have no width size hint. Without a real minimum,
        # Qt may give a time-series view zero width beside an expanding canvas.
        widget.setMinimumWidth(max(300, widget.minimumWidth()))
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.body.addWidget(widget, stretch)
        widget.show()
        return widget

    def restore_views(self):
        for widget, parent, layout, index, stretch, hidden, min_width, policy in reversed(
            self.borrowed
        ):
            self.body.removeWidget(widget)
            widget.setParent(parent)
            widget.setMinimumWidth(min_width)
            widget.setSizePolicy(policy)
            if isinstance(parent, QSplitter):
                parent.insertWidget(max(0, index), widget)
            elif layout is not None and hasattr(layout, "insertWidget"):
                layout.insertWidget(max(0, index), widget, stretch)
            widget.setVisible(not hidden)
        self.borrowed.clear()
        for splitter, sizes in self.splitter_sizes.items():
            splitter.setSizes(sizes)
        self.splitter_sizes.clear()

    def done(self, result):
        # Escape and accept/reject hide QDialog without necessarily closing it.
        self.restore_views()
        super().done(result)

    def closeEvent(self, event):
        self.restore_views()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        fit_to_screen(self, 1380, 720)


def open_current_results(main):
    """Public GUI command: inspect current results without scrolling the whole page."""
    existing = getattr(main, "_result_focus", None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return
    physics = main.tabs.currentWidget() is main.physics_panel
    result = main.physics_panel.result if physics else main.current_result
    if not result:
        main.statusBar().showMessage("请先加载水力或三维物理结果，再使用成果聚焦。", 7000)
        return
    dialog = ResultFocus(main, "成果聚焦", "主要图像与曲线固定同屏，关闭后返回原页面")
    if physics:
        dialog.add_view(main.physics_panel.field, 3, borrow=True)
        dialog.add_view(main.physics_panel.plot, 2, borrow=True)
        dialog.note.setText(main.physics_panel.assessment.text())
    else:
        dialog.add_view(main.result_canvas, 3, borrow=True)
        dialog.add_view(main.trend, 2, borrow=True)
        s = result["summary"]
        dialog.set_cards(
            [
                ("期间供水", f"{s['delivered_m3']:.2f} m³", "当前加载工况"),
                ("期间能耗", f"{s['energy_kwh']:.3f} kWh", "泵组电输入"),
                ("未满足需水", f"{s['unmet_m3']:.3f} m³", "结合压力约束判断"),
            ]
        )
        dialog.note.setText("计算结果用于当前模型；实际设备结论需要现场数据校准。")
    slider = main.physics_panel.time if physics else main.timeline
    from PySide6.QtWidgets import QSlider

    time = QSlider(Qt.Orientation.Horizontal)
    time.setRange(slider.minimum(), slider.maximum())
    time.setValue(slider.value())
    time.valueChanged.connect(slider.setValue)
    dialog.controls.addWidget(QLabel("时间回放"))
    dialog.controls.addWidget(time, 1)
    main._result_focus = dialog
    dialog.show()
    dialog.raise_()
