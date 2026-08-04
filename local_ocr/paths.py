"""Well-known locations, resolved relative to the repository root.

Works both as a normal Python package and as a PyInstaller-frozen ``.exe``:

* **Read-only resources** (bundled config, static UI) resolve from the bundle.
  When frozen, PyInstaller unpacks them under ``sys._MEIPASS``; in development
  they sit in the repo.
* **Writable runtime data** (the saved settings, the ``output/`` folder) resolve
  next to the executable when frozen, so results are where the user can find
  them and settings survive a restart. In development that is still the repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FROZEN = getattr(sys, "frozen", False)

#: Read-only resources: PyInstaller's temp unpack dir when frozen, else the repo.
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
#: Writable data: beside the .exe when frozen, else the repo.
DATA_ROOT = (
    Path(sys.executable).resolve().parent if _FROZEN else Path(__file__).resolve().parent.parent
)

# Kept as the read-only root for backwards compatibility with existing imports.
REPO_ROOT = BUNDLE_ROOT
CONFIG_DIR = BUNDLE_ROOT / "config"
SAMPLES_DIR = BUNDLE_ROOT / "samples"
STATIC_DIR = BUNDLE_ROOT / "app" / "static"

PREFIX_WHITELIST = CONFIG_DIR / "meter_prefixes.txt"
SECTION_MARKERS = CONFIG_DIR / "section_markers.txt"
SPECIAL_TOKENS = CONFIG_DIR / "special_tokens.yaml"
SETTINGS = CONFIG_DIR / "settings.yaml"

GROUND_TRUTH = SAMPLES_DIR / "ground_truth.csv"
PAGE_MANIFEST = SAMPLES_DIR / "page_manifest.yaml"

# --- writable runtime locations -------------------------------------------
OUTPUT_DIR = DATA_ROOT / "output"
#: Where the app persists the Gemini key / model / mode. In development this
#: stays in ``config/`` (unchanged); a frozen build writes beside the .exe.
APP_SETTINGS_PATH = (
    (DATA_ROOT / "app_settings.json") if _FROZEN else (CONFIG_DIR / "app_settings.json")
)
