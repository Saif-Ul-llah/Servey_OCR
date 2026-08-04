# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Servey OCR desktop app.

Produces a single self-contained ``ServeyOCR.exe``. Build with:

    python -m pip install pyinstaller
    python -m PyInstaller ServeyOCR.spec --noconfirm

The read-only resources (config tables, the web UI) are bundled inside the exe;
the Gemini key and the ``output/`` folder are written next to the exe at runtime
(see local_ocr/paths.py).
"""

from PyInstaller.utils.hooks import collect_submodules

# uvicorn resolves its event-loop and protocol implementations dynamically, so
# PyInstaller cannot see them by following imports -- collect them explicitly.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("anyio")
    + ["multipart", "python_multipart"]
)

datas = [
    ("config/meter_prefixes.txt", "config"),
    ("config/section_markers.txt", "config"),
    ("config/special_tokens.yaml", "config"),
    ("config/settings.yaml", "config"),
    ("app/static", "app/static"),
]

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the exe lean: the desktop app never touches these heavy libs.
    excludes=["torch", "transformers", "paddleocr", "paddlepaddle", "easyocr",
              "matplotlib", "cv2", "tkinter", "PySide6", "PyQt5"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ServeyOCR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # a small status window; users see the URL and any errors
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
