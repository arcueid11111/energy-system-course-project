#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
    task_python="${FLOWSTATE_PYTHON:-python3.12}"
    "$task_python" -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python gui.py
