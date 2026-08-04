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

echo.
echo Which build?
echo   [1] Lean        - cloud only (Gemini/Grok), ~45 MB, fast
echo   [2] Offline     - adds local OCR (OpenCV + EasyOCR), ~1-2 GB, slow build
echo   [3] Both
set /p CHOICE="Enter 1, 2 or 3 (default 1): "
if "%CHOICE%"=="" set CHOICE=1

if "%CHOICE%"=="1" goto lean
if "%CHOICE%"=="3" goto lean
if "%CHOICE%"=="2" goto offline
goto lean

:lean
echo Building ServeyOCR.exe ...
"%PY%" -m PyInstaller ServeyOCR.spec --noconfirm --distpath dist --workpath build
if "%CHOICE%"=="3" goto offline
goto done

:offline
echo Building ServeyOCR-Offline.exe ... this takes several minutes.
"%PY%" -m PyInstaller ServeyOCR-Offline.spec --noconfirm --distpath dist --workpath build
goto done

:done
echo.
echo Done. Executables are in dist\
pause
