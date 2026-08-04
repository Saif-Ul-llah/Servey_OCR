@echo off
REM Rebuild the single-file ServeyOCR.exe from source.
REM Only needed after changing the app; the built exe lives in dist\.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Installing PyInstaller (if needed)...
"%PY%" -m pip install --quiet pyinstaller

echo Building ServeyOCR.exe ...
"%PY%" -m PyInstaller ServeyOCR.spec --noconfirm --distpath dist --workpath build

echo.
echo Done. The app is at dist\ServeyOCR.exe
pause
