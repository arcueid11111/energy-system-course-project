@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto :failed
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe gui.py
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Install Python 3.12 and retry, or download the ready-to-run Windows release.
pause
exit /b 1
