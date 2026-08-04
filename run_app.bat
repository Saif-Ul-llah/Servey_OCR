@echo off
REM Double-click launcher for the Servey OCR app.
REM Uses the local virtual environment if present, otherwise the system Python.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Starting Servey OCR...
"%PY%" run_app.py

if errorlevel 1 (
    echo.
    echo The app exited with an error. If this is the first run, install the
    echo dependencies first:  pip install -r requirements.txt
    pause
)
