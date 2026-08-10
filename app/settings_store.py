"""Persisted, user-editable app settings.

Holds the Gemini API key, the model name, and the recognition mode, in a small
JSON file next to the other config. This is a single-user local desktop tool,
so the key is stored in plaintext on the user's own machine -- the same trust
model as a `.env` file. It is never sent anywhere except to Google's API.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from local_ocr.paths import APP_SETTINGS_PATH

SETTINGS_PATH = APP_SETTINGS_PATH

#: Gemini vision-capable model used by default. Flash is fast and cheap and
#: reads this block hand well; the user can change it in the settings panel.
DEFAULT_MODEL = "gemini-2.0-flash"

#: xAI Grok vision model. Verified against the live API: the older
#: ``grok-2-vision-*`` and ``grok-beta`` names are retired and now 404, while
#: ``grok-4*`` and ``grok-3`` resolve. Names change often, so the UI takes a
#: free-text value with suggestions rather than a closed dropdown, and the
#: Test button confirms the name against the account actually being used.
DEFAULT_GROK_MODEL = "grok-4-fast"

#: Local (offline) OCR engine used when mode == "local".
#:
#: EasyOCR, not TrOCR, despite TrOCR being the "handwriting" model. Measured on
#: page_08 against the golden set: EasyOCR read 3 meter codes exactly and 5 of 7
#: digit strings, TrOCR-base none and 3 of 7 -- and EasyOCR is ~6x faster
#: (0.65s vs ~4s per cell, i.e. a page in a minute instead of ten). The reason
#: is the hand itself: these are neat block capitals and digits, closer to the
#: printed signage EasyOCR was trained on than to the English prose TrOCR
#: expects. EasyOCR also takes the field alphabet as a decoder allowlist, and
#: its confidence tracks correctness closely enough to drive review flagging.
DEFAULT_LOCAL_ENGINE = "easyocr"

#: Model names passed to an agent CLI's ``--model`` flag. Free text like the
#: cloud providers' -- these churn just as fast, and each CLI also accepts its
#: own aliases ("opus", "sonnet"). Blank means "whatever the CLI is configured
#: with", which is the right default for a tool the user already set up.
DEFAULT_CLAUDECODE_MODEL = "claude-opus-5"
DEFAULT_CODEX_MODEL = ""

#: "gemini"/"grok" -> cloud recognition; "local" -> offline OCR engine + rules;
#: "claudecode"/"codex" -> drive an agent CLI installed on this machine, which
#: carries its own login instead of an API key stored here.
VALID_MODES = ("gemini", "grok", "local", "claudecode", "codex")
#: Modes served by ``app.agent_cli`` rather than ``app.recognizer``.
AGENT_MODES = ("claudecode", "codex")
#: Local OCR by default -- no key, no network -- but only where the imaging
#: stack is actually present. The lean .exe ships without it and must not open
#: in a mode it cannot run, while the offline .exe bundles it and should. Probe
#: rather than assume from ``sys.frozen``: both kinds of build exist.
DEFAULT_MODE = "local" if importlib.util.find_spec("cv2") is not None else "gemini"


@dataclass
class AppSettings:
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_MODEL
    grok_api_key: str = ""
    grok_model: str = DEFAULT_GROK_MODEL
    local_engine: str = DEFAULT_LOCAL_ENGINE
    claudecode_model: str = DEFAULT_CLAUDECODE_MODEL
    codex_model: str = DEFAULT_CODEX_MODEL
    #: Local OCR is the default: it works out of the box with no API key, and
    #: needs no network. The cloud modes are more accurate but must be enabled
    #: by pasting a key into Settings.
    mode: str = DEFAULT_MODE

    @property
    def has_key(self) -> bool:
        """Whether the *currently selected* cloud provider has a key."""
        return self.has_key_for(self.mode if self.mode in ("gemini", "grok") else "gemini")

    def has_key_for(self, provider: str) -> bool:
        return bool(self.key_for(provider).strip())

    def key_for(self, provider: str) -> str:
        return self.grok_api_key if provider == "grok" else self.gemini_api_key

    def model_for(self, provider: str) -> str:
        if provider == "grok":
            return self.grok_model
        if provider == "claudecode":
            return self.claudecode_model
        if provider == "codex":
            return self.codex_model
        return self.gemini_model

    def public(self) -> dict:
        """Safe-to-serialise view: never leak the keys themselves to the frontend."""
        return {
            "gemini_model": self.gemini_model,
            "grok_model": self.grok_model,
            "local_engine": self.local_engine,
            "claudecode_model": self.claudecode_model,
            "codex_model": self.codex_model,
            "mode": self.mode,
            "has_key": self.has_key,
            "has_gemini_key": self.has_key_for("gemini"),
            "has_grok_key": self.has_key_for("grok"),
            "default_model": DEFAULT_MODEL,
            "default_grok_model": DEFAULT_GROK_MODEL,
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
        grok_api_key=str(data.get("grok_api_key", "") or ""),
        grok_model=str(data.get("grok_model") or DEFAULT_GROK_MODEL),
        local_engine=str(data.get("local_engine") or DEFAULT_LOCAL_ENGINE),
        claudecode_model=str(data.get("claudecode_model") or DEFAULT_CLAUDECODE_MODEL),
        # Blank is a meaningful value here ("use the CLI's own default"), so an
        # absent key falls back to the default but a saved "" is kept.
        codex_model=str(data.get("codex_model", DEFAULT_CODEX_MODEL) or ""),
        mode=str(data.get("mode") or DEFAULT_MODE),
    )
    # The environment fills a blank key only, so a key typed into the UI is
    # never shadowed by a stale shell variable.
    for attribute, variable in (
        ("gemini_api_key", "GEMINI_API_KEY"),
        ("grok_api_key", "XAI_API_KEY"),
    ):
        if not getattr(settings, attribute).strip():
            env_key = os.environ.get(variable, "").strip()
            if env_key:
                setattr(settings, attribute, env_key)
    if settings.mode == "manual":  # legacy value -> the offline OCR mode
        settings.mode = "local"
    if settings.mode not in VALID_MODES:
        settings.mode = DEFAULT_MODE
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
    grok_api_key: str | None = None,
    grok_model: str | None = None,
    local_engine: str | None = None,
    claudecode_model: str | None = None,
    codex_model: str | None = None,
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
    if grok_api_key is not None and grok_api_key.strip():
        settings.grok_api_key = grok_api_key.strip()
    if grok_model is not None and grok_model.strip():
        settings.grok_model = grok_model.strip()
    if local_engine is not None and local_engine.strip():
        settings.local_engine = local_engine.strip()
    # Agent models are the one field where blank is a real value, not "no
    # change": it means "let the CLI pick", so an emptied box must be able to
    # get back there. Safe to honour here because these are not secrets -- the
    # blank-means-no-change rule above exists to stop a saved key being wiped.
    if claudecode_model is not None:
        settings.claudecode_model = claudecode_model.strip()
    if codex_model is not None:
        settings.codex_model = codex_model.strip()
    if mode is not None and mode in VALID_MODES:
        settings.mode = mode
    save(settings)
    return settings


def clear_key() -> AppSettings:
    settings = load()
    settings.gemini_api_key = ""
    save(settings)
    return settings
