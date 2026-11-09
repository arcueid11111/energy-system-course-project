#!/usr/bin/env python3
"""Offline desktop GUI.

python gui.py
python gui.py --project examples/editable_network.json
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="FLOWSTATE 二维/三维管网设计与数字孪生工作台")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--self-check", type=Path, metavar="OUTPUT_JSON")
    args = parser.parse_args()
    if args.self_check:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["MPLBACKEND"] = "Agg"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtCore import QStandardPaths
    except ImportError:
        raise SystemExit("缺少图形依赖，请运行：python -m pip install -r requirements.txt")
    from desktop.window import MainWindow

    from desktop.environment import prepare_qt_plugins
    prepare_qt_plugins()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("FLOWSTATE")
    app.setOrganizationName("Flowstate")
    if args.self_check:
        from desktop.checks import self_check

        return self_check(app, args.self_check)
    folder = args.data_dir or Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    try:
        window = MainWindow(folder)
    except Exception as exc:
        QMessageBox.critical(None, "FLOWSTATE 启动失败", str(exc))
        return 1
    if args.project:
        window.load_project(args.project)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
