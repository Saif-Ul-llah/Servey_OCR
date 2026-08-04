"""Cloud recognition with Google Gemini.

The local pipeline was designed around a swappable recognizer (see
``local_ocr/ocr/base.py``). This module is the cloud implementation: it hands a
whole page photograph to a Gemini vision model and asks for the handwritten
table back as structured JSON -- one object per ruled line, values transcribed
exactly as written. Layout and reading happen together in the model, which is
why this replaces both the (fragile) local segmenter and a local OCR engine.

Everything downstream is unchanged: the raw strings returned here flow into the
same deterministic correction -> assembly -> export core as before.

No SDK dependency: this speaks the REST API directly with ``requests`` so the
install stays tiny and offline-friendly except for the one call that needs the
network.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_TIMEOUT = 180  # seconds; a cold Flash call on a dense page is well under this

#: What we ask the model to produce. Kept deliberately close to the notebook's
#: own conventions so the raw output needs as little repair as possible -- but
#: repair still runs, so "exactly as written" is safe to insist on here.
PROMPT = """\
You are transcribing one page of a handwritten field survey notebook. The page
is a three-column table. For every ruled line that has any handwriting, output
one JSON object with these string fields:

- "survey":  the survey/plot number in the leftmost column, as DIGITS ONLY
  (e.g. "683", not "KE-683" -- drop any KE- prefix). Most lines leave this
  BLANK because they continue the plot named on a line above -- output "" there.
- "meter":   the meter code in the middle column. Almost always 2-3 uppercase
  letters followed by 5 digits (e.g. "SCT12345", "KE45386"). It may instead be
  a ditto/repeat mark (a quote-like " or -"-) or a note like "No meter".
- "remarks": anything in the right column: side codes written like +37 or #37,
  and/or short English notes ("Shop", "2 Shops"). Output "" if empty. Keep side
  codes exactly as written, including their + or # sign.

Rules:
- Transcribe EXACTLY what is written. Do not invent, correct, or complete codes.
- Preserve the top-to-bottom order of the lines.
- Do not skip blank-survey continuation lines -- they carry their own meter.
- Ignore printed header text, page numbers, and any non-Latin (Urdu) script.
- Return ONLY a JSON object: {"rows": [ {"survey": "...", "meter": "...", "remarks": "..."}, ... ]}
"""


@dataclass
class RawRow:
    """One line as the model read it, before any correction."""

    survey: str = ""
    meter: str = ""
    remarks: str = ""


@dataclass
class RecognitionResult:
    page: str
    rows: list[RawRow] = field(default_factory=list)
    error: str | None = None


class GeminiError(RuntimeError):
    """A recognition call failed in a way the user needs to see and act on."""


def _encode_image(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime, data


def _extract_rows(payload: dict) -> list[RawRow]:
    """Pull the row list out of a Gemini response, tolerating stray prose.

    ``response_mime_type=application/json`` makes the model return clean JSON,
    but we still guard against a code fence or leading sentence so a good page
    is never lost to a formatting quirk.
    """
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError(f"unexpected response shape: {exc}") from exc

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise GeminiError("model did not return JSON")
        obj = json.loads(match.group(0))

    raw_rows = obj.get("rows", obj) if isinstance(obj, dict) else obj
    if not isinstance(raw_rows, list):
        raise GeminiError("JSON did not contain a rows array")

    rows: list[RawRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        rows.append(
            RawRow(
                survey=str(item.get("survey", "") or "").strip(),
                meter=str(item.get("meter", "") or "").strip(),
                remarks=str(item.get("remarks", "") or "").strip(),
            )
        )
    return rows


def recognise_page(path: Path, api_key: str, model: str) -> RecognitionResult:
    """Recognise one page image. Never raises -- errors ride on the result."""
    page = path.stem
    if not api_key:
        return RecognitionResult(page=page, error="No Gemini API key configured.")

    try:
        mime, data = _encode_image(path)
    except OSError as exc:
        return RecognitionResult(page=page, error=f"could not read image: {exc}")

    body = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime, "data": data}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    url = f"{API_ROOT}/{model}:generateContent"

    try:
        resp = requests.post(
            url, params={"key": api_key}, json=body, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        return RecognitionResult(page=page, error=f"network error: {exc}")

    if resp.status_code != 200:
        return RecognitionResult(page=page, error=_http_error(resp))

    try:
        rows = _extract_rows(resp.json())
    except (GeminiError, ValueError) as exc:
        return RecognitionResult(page=page, error=str(exc))

    return RecognitionResult(page=page, rows=rows)


def _http_error(resp: requests.Response) -> str:
    """Turn a non-200 into a message a user can act on."""
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except ValueError:
        detail = resp.text[:200]
    if resp.status_code in (401, 403):
        return f"Gemini rejected the API key ({resp.status_code}). {detail}"
    if resp.status_code == 404:
        return f"Model not found ({resp.status_code}). Check the model name. {detail}"
    if resp.status_code == 429:
        return "Gemini rate limit / quota exceeded (429). Try again shortly."
    return f"Gemini error {resp.status_code}: {detail}"


def test_key(api_key: str, model: str) -> tuple[bool, str]:
    """Cheap text-only call to verify a key + model before real work.

    Returns ``(ok, message)``. Used by the settings panel's "Test" button.
    """
    if not api_key:
        return False, "No API key provided."
    url = f"{API_ROOT}/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": "Reply with the single word: ok"}]}]}
    try:
        resp = requests.post(url, params={"key": api_key}, json=body, timeout=30)
    except requests.RequestException as exc:
        return False, f"Network error: {exc}"
    if resp.status_code == 200:
        return True, f"Key works with {model}."
    return False, _http_error(resp)
