# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the **offline** build of Servey OCR.

Produces ``ServeyOCR-Offline.exe``: everything the lean build has, plus the
whole local OCR stack -- OpenCV, PyTorch and EasyOCR, with EasyOCR's weights
baked in so the first run does not quietly download 94 MB. That makes the
executable large (expect ~1-2 GB); the lean ``ServeyOCR.spec`` stays the choice
when only the cloud modes are needed.

Build with:

    python -m pip install pyinstaller
    python -m PyInstaller ServeyOCR-Offline.spec --noconfirm

The weights are taken from EasyOCR's own cache (``~/.EasyOCR/model``), so run
the app from source once before building if that folder is empty.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# uvicorn resolves its event loop and protocol implementations dynamically.
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
binaries = []

# The OCR stack. `collect_all` is required rather than plain imports: these
# packages load submodules, compiled extensions and data files dynamically, so
# following imports alone leaves the frozen build broken at runtime.
for package in ("easyocr", "torch", "torchvision", "cv2", "skimage", "scipy", "PIL"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception:  # noqa: BLE001 - an absent optional package is not fatal
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# EasyOCR's pretrained weights, so the offline build is genuinely offline.
_weights = Path.home() / ".EasyOCR" / "model"
if _weights.is_dir():
    datas += [(str(path), "easyocr_models") for path in _weights.glob("*.pth")]
else:  # pragma: no cover - build-time guidance only
    print(
        "WARNING: no EasyOCR weights found at %s -- the built exe will try to "
        "download them on first use. Run the app once from source first." % _weights
    )

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Kept out: the TrOCR path (transformers) is not the default engine and
    # would add gigabytes again, and these are developer-only tools.
    excludes=["transformers", "paddleocr", "paddlepaddle", "matplotlib",
              "tkinter", "PySide6", "PyQt5", "pytest", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ServeyOCR-Offline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off here: it makes an already slow first start much slower on a
    # bundle this size, and can corrupt some native extensions.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
