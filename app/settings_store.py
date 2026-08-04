"""Persisted, user-editable app settings.

Holds the Gemini API key, the model name, and the recognition mode, in a small
JSON file next to the other config. This is a single-user local desktop tool,
so the key is stored in plaintext on the user's own machine -- the same trust
model as a `.env` file. It is never sent anywhere except to Google's API.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from local_ocr.paths import APP_SETTINGS_PATH

SETTINGS_PATH = APP_SETTINGS_PATH

#: Gemini vision-capable model used by default. Flash is fast and cheap and
#: reads this block hand well; the user can change it in the settings panel.
DEFAULT_MODEL = "gemini-2.0-flash"

VALID_MODES = ("gemini", "manual")


@dataclass
class AppSettings:
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_MODEL
    #: "gemini" -> auto-recognise with the cloud model; "manual" -> type by hand.
    mode: str = "gemini"

    @property
    def has_key(self) -> bool:
        return bool(self.gemini_api_key.strip())

    def public(self) -> dict:
        """Safe-to-serialise view: never leak the key itself to the frontend."""
        return {
            "gemini_model": self.gemini_model,
            "mode": self.mode,
            "has_key": self.has_key,
            "default_model": DEFAULT_MODEL,
        }


def load() -> AppSettings:
    """Read settings from disk, falling back to the GEMINI_API_KEY env var.

    The environment wins only when the stored key is blank, so a key typed into
    the UI is not silently shadowed by a stale shell variable.
    """
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    settings = AppSettings(
        gemini_api_key=str(data.get("gemini_api_key", "") or ""),
        gemini_model=str(data.get("gemini_model") or DEFAULT_MODEL),
        mode=str(data.get("mode") or "gemini"),
    )
    if not settings.has_key:
        env_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if env_key:
            settings.gemini_api_key = env_key
    if settings.mode not in VALID_MODES:
        settings.mode = "gemini"
    return settings


def save(settings: AppSettings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def update(
    *,
    gemini_api_key: str | None = None,
    gemini_model: str | None = None,
    mode: str | None = None,
) -> AppSettings:
    """Apply a partial change and persist. Blank/omitted fields are left as-is.

    A blank ``gemini_api_key`` string is treated as "no change", so saving the
    model or mode from the UI does not wipe a previously stored key. Clearing
    the key is done through :func:`clear_key`.
    """
    settings = load()
    if gemini_api_key is not None and gemini_api_key.strip():
        settings.gemini_api_key = gemini_api_key.strip()
    if gemini_model is not None and gemini_model.strip():
        settings.gemini_model = gemini_model.strip()
    if mode is not None and mode in VALID_MODES:
        settings.mode = mode
    save(settings)
    return settings


def clear_key() -> AppSettings:
    settings = load()
    settings.gemini_api_key = ""
    save(settings)
    return settings
