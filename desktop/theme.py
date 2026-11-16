"""Shared colours and the application style sheet."""

ACCENT = "#1f7a6a"
ACCENT_DARK = "#17604f"
ACCENT_SOFT = "#e3f1ec"
HIGHLIGHT = "#d48b2c"
HIGHLIGHT_SOFT = "#f0c27e"
CANVAS = "#f9fbfa"
INK = "#1f3a31"
MUTED = "#6c8579"
LINE = "#d7e3dc"
SURFACE = "#ffffff"
BACKGROUND = "#f1f5f3"
DANGER = "#c8553d"
WARNING = "#e39b3b"
INFO = "#3b78b4"

STATE_COLORS = {
    "queued": ("#eef2f0", "#5f776b"),
    "running": ("#e3eefa", "#2f63a0"),
    "cancelling": ("#fbf0dd", "#9a6416"),
    "succeeded": ("#e3f3ea", "#22734d"),
    "failed": ("#fbe6e2", "#a33d27"),
    "cancelled": ("#eef0f2", "#6b7280"),
}
SEVERITY_COLORS = {"error": DANGER, "warning": WARNING, "info": INFO}

MONO = "'SF Mono','Menlo','Consolas','DejaVu Sans Mono',monospace"

STYLE = f"""
QMainWindow, QDialog {{ background:{BACKGROUND}; color:{INK}; }}
QWidget {{ font-size:13px; }}
QLabel {{ color:{MUTED}; }}
QLabel#brand {{ font-size:17px; font-weight:700; color:{ACCENT_DARK}; padding:0 8px 0 4px; letter-spacing:1px; }}
QLabel#brandSub {{ font-size:12px; color:{MUTED}; padding-left:2px; }}
QLabel#inspectorTitle {{ font-size:19px; font-weight:600; color:{INK}; padding:4px 0 6px 0; }}
QLabel#sectionTitle {{ font-size:15px; font-weight:600; color:{INK}; padding:2px 0 4px 0; }}
QLabel#hint {{ color:#86a093; font-size:12px; }}
QLabel#summary {{ background:{ACCENT_SOFT}; color:#2c5a48; padding:10px 13px; border-radius:8px; }}
QLabel#pill {{ background:{ACCENT_SOFT}; color:{ACCENT_DARK}; border-radius:10px; padding:3px 10px; font-size:12px; }}
QLabel#dirtyPill {{ background:#fbf0dd; color:#9a6416; border-radius:10px; padding:3px 10px; font-size:12px; }}
QFrame#card {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:10px; }}
QLabel#cardTitle {{ color:{MUTED}; font-size:12px; }}
QLabel#cardValue {{ color:{INK}; font-size:20px; font-weight:600; }}
QLabel#cardUnit {{ color:{MUTED}; font-size:12px; }}
QPushButton, QToolButton {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:6px;
    padding:6px 12px; color:#2f5d4b; }}
QPushButton:hover, QToolButton:hover {{ background:#eaf3ee; border-color:#bcd4c6; }}
QPushButton:pressed, QToolButton:pressed {{ background:#dcebe3; }}
QPushButton:disabled {{ color:#acb9b0; background:#f6f8f7; }}
QPushButton#primary {{ background:{ACCENT}; color:white; border:1px solid {ACCENT_DARK}; font-weight:600; }}
QPushButton#primary:hover {{ background:{ACCENT_DARK}; }}
QPushButton#primary:disabled {{ background:#9dbfb5; border-color:#9dbfb5; }}
QToolButton#tool {{ padding:5px 10px; border-radius:0; border-right-width:0; }}
QToolButton#tool:checked {{ background:{ACCENT}; color:white; border-color:{ACCENT_DARK}; }}
QToolButton#toolFirst {{ padding:5px 10px; border-top-right-radius:0; border-bottom-right-radius:0; border-right-width:0; }}
QToolButton#toolFirst:checked {{ background:{ACCENT}; color:white; border-color:{ACCENT_DARK}; }}
QToolButton#toolLast {{ padding:5px 10px; border-top-left-radius:0; border-bottom-left-radius:0; }}
QToolButton#toolLast:checked {{ background:{ACCENT}; color:white; border-color:{ACCENT_DARK}; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background:{SURFACE}; border:1px solid {LINE}; border-radius:5px; padding:4px 6px;
    color:#24483b; selection-background-color:#b5d2bb; selection-color:{INK}; }}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border:1px solid {ACCENT}; }}
QDoubleSpinBox[changed="true"], QLineEdit[changed="true"] {{ background:#fff8ec; border-color:{HIGHLIGHT_SOFT}; }}
QPlainTextEdit#code, QPlainTextEdit#log {{ font-family:{MONO}; font-size:12px; }}
QPlainTextEdit#log {{ background:#fbfcfb; color:#4b6a5d; }}
QTabWidget::pane {{ border:1px solid {LINE}; border-radius:8px; background:{SURFACE}; top:-1px; }}
QTabBar::tab {{ padding:9px 18px; color:#728b7f; background:transparent; border:0;
    border-bottom:2px solid transparent; margin-right:2px; }}
QTabBar::tab:hover {{ color:{INK}; }}
QTabBar::tab:selected {{ color:{ACCENT_DARK}; border-bottom:2px solid {ACCENT}; font-weight:600; }}
QToolBar {{ background:{SURFACE}; border-bottom:1px solid {LINE}; padding:4px 8px; spacing:4px; }}
QToolBar QToolButton {{ border:1px solid transparent; background:transparent; padding:5px 9px; }}
QToolBar QToolButton:hover {{ background:#eaf3ee; border-color:{LINE}; }}
QMenuBar {{ background:{SURFACE}; }}
QMenu {{ background:{SURFACE}; border:1px solid {LINE}; padding:4px; }}
QMenu::item {{ padding:6px 22px; border-radius:4px; }}
QMenu::item:selected {{ background:{ACCENT_SOFT}; color:{INK}; }}
QTableWidget, QTreeWidget, QListWidget {{ background:{SURFACE}; border:1px solid {LINE};
    border-radius:6px; gridline-color:#edf2ee; alternate-background-color:#f8fbf9; }}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{
    background:#d7ebe2; color:{INK}; }}
QHeaderView::section {{ background:#f2f7f4; padding:7px 8px; border:0; border-bottom:1px solid {LINE};
    color:#627d70; font-weight:600; }}
QProgressBar {{ border:0; background:#e9f0ec; border-radius:4px; text-align:center; height:12px; font-size:10px; color:#3c5f51; }}
QProgressBar::chunk {{ background:#8fcbb6; border-radius:4px; }}
QScrollArea {{ border:0; background:transparent; }}
QSplitter::handle {{ background:transparent; }}
QSplitter::handle:hover {{ background:#dbe7e0; }}
QSlider::groove:horizontal {{ height:4px; background:#dbe7e0; border-radius:2px; }}
QSlider::sub-page:horizontal {{ background:{ACCENT}; border-radius:2px; }}
QSlider::handle:horizontal {{ background:white; border:2px solid {ACCENT}; width:12px; height:12px;
    margin:-6px 0; border-radius:8px; }}
QStatusBar {{ background:{SURFACE}; border-top:1px solid {LINE}; color:{MUTED}; }}
QStatusBar QLabel {{ padding:0 8px; }}
QGroupBox {{ border:1px solid {LINE}; border-radius:8px; margin-top:14px; padding:10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 4px; color:{MUTED}; }}
QCheckBox {{ color:#3d6152; }}
QToolTip {{ background:white; color:{INK}; border:1px solid {LINE}; padding:6px; border-radius:4px; }}
"""
