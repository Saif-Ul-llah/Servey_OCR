"""Cloud recognition: Google Gemini and xAI Grok.

Both providers do the same job -- hand a whole page photograph to a vision model
and get the handwritten table back as structured JSON, one object per ruled
line. Doing layout and reading in one call is why the cloud path avoids the
local segmenter entirely, and why it is the accurate option on these images.

Only the transport differs, so the prompt, the JSON parsing and the error
handling are shared and each provider is a thin request builder:

* **Gemini** -- ``generativelanguage.googleapis.com``, key as a query parameter,
  image as ``inline_data``.
* **Grok** -- ``api.x.ai``, an OpenAI-compatible chat endpoint, bearer token,
  image as a ``data:`` URL.

No SDKs: both are plain ``requests`` calls, which keeps the install small and
the failure modes visible.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
GROK_URL = "https://api.x.ai/v1/chat/completions"
REQUEST_TIMEOUT = 180  # seconds; a dense page is comfortably under this

PROVIDERS = ("gemini", "grok")

#: What we ask for. Kept close to the notebook's own conventions so the raw
#: output needs as little repair as possible -- but repair still runs, so
#: insisting on "exactly as written" here is safe.
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


class CloudError(RuntimeError):
    """A recognition call failed in a way the user needs to see and act on."""


def _encode_image(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def _rows_from_text(text: str) -> list[RawRow]:
    """Parse the model's reply into rows, tolerating fences and stray prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise CloudError("model did not return JSON")
        obj = json.loads(match.group(0))

    raw_rows = obj.get("rows", obj) if isinstance(obj, dict) else obj
    if not isinstance(raw_rows, list):
        raise CloudError("JSON did not contain a rows array")

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


# ------------------------------------------------------------------ providers


def _gemini_request(path: Path, api_key: str, model: str) -> requests.Response:
    mime, data = _encode_image(path)
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
    return requests.post(
        f"{GEMINI_ROOT}/{model}:generateContent",
        params={"key": api_key},
        json=body,
        timeout=REQUEST_TIMEOUT,
    )


def _gemini_text(payload: dict) -> str:
    try:
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CloudError(f"unexpected Gemini response shape: {exc}") from exc


def _grok_request(path: Path, api_key: str, model: str) -> requests.Response:
    mime, data = _encode_image(path)
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                ],
            }
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    return requests.post(
        GROK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=REQUEST_TIMEOUT,
    )


def _grok_text(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CloudError(f"unexpected Grok response shape: {exc}") from exc


_PROVIDERS = {
    "gemini": (_gemini_request, _gemini_text, "Gemini"),
    "grok": (_grok_request, _grok_text, "Grok"),
}


def _http_error(resp: requests.Response, label: str) -> str:
    """Turn a non-200 into a message a user can act on."""
    try:
        payload = resp.json()
        detail = payload.get("error", {})
        detail = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    except ValueError:
        detail = resp.text[:200]
    if resp.status_code in (401, 403):
        return f"{label} rejected the API key ({resp.status_code}). {detail}"
    if resp.status_code == 404:
        return f"{label} model not found ({resp.status_code}). Check the model name. {detail}"
    if resp.status_code == 429:
        return f"{label} rate limit / quota exceeded (429). Try again shortly."
    return f"{label} error {resp.status_code}: {detail}"


# -------------------------------------------------------------------- public


def recognise_page(
    path: Path, api_key: str, model: str, provider: str = "gemini"
) -> RecognitionResult:
    """Recognise one page image. Never raises -- errors ride on the result."""
    page = Path(path).stem
    if provider not in _PROVIDERS:
        return RecognitionResult(page=page, error=f"unknown provider {provider!r}")
    build, extract, label = _PROVIDERS[provider]

    if not api_key:
        return RecognitionResult(page=page, error=f"No {label} API key configured.")
    if not Path(path).exists():
        return RecognitionResult(page=page, error="image file is missing")

    try:
        resp = build(Path(path), api_key, model)
    except OSError as exc:
        return RecognitionResult(page=page, error=f"could not read image: {exc}")
    except requests.RequestException as exc:
        return RecognitionResult(page=page, error=f"network error: {exc}")

    if resp.status_code != 200:
        return RecognitionResult(page=page, error=_http_error(resp, label))

    try:
        rows = _rows_from_text(extract(resp.json()))
    except (CloudError, ValueError) as exc:
        return RecognitionResult(page=page, error=str(exc))

    return RecognitionResult(page=page, rows=rows)


def test_key(api_key: str, model: str, provider: str = "gemini") -> tuple[bool, str]:
    """Cheap text-only call to verify a key + model. Returns ``(ok, message)``."""
    if provider not in _PROVIDERS:
        return False, f"Unknown provider {provider!r}."
    _, _, label = _PROVIDERS[provider]
    if not api_key:
        return False, "No API key provided."

    try:
        if provider == "gemini":
            resp = requests.post(
                f"{GEMINI_ROOT}/{model}:generateContent",
                params={"key": api_key},
                json={"contents": [{"parts": [{"text": "Reply with the single word: ok"}]}]},
                timeout=30,
            )
        else:
            resp = requests.post(
                GROK_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                },
                timeout=30,
            )
    except requests.RequestException as exc:
        return False, f"Network error: {exc}"

    if resp.status_code == 200:
        return True, f"Key works with {model}."
    return False, _http_error(resp, label)
