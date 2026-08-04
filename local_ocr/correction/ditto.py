"""Repeat-mark ("ditto") expansion, for both the METER# and REMARKS columns.

Two behaviours, both verified against the sample pages:

**METER# column.** A mark followed by five digits means "same prefix as the line
above". The inherited prefix comes from the *physically preceding line*, which
is not necessarily the same survey group -- on page_09 the line ``" 45386``
belongs to KE-662 but takes its ``SCT`` from KE-661's ``SCT89642``. Resetting
the inherited prefix at group boundaries would get that row wrong.

**REMARKS column.** A mark alone means "same remark as above". On page_03,
``Imam Bargah`` is written once with a mark beneath it and the workbook carries
the text on both KE-602 and KE-603.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from local_ocr.correction.text_rules import DITTO_CHARS, collapse, is_ditto

#: A repeat mark, then a run of digits: ``" 70028``, ``x 96344``, ``~ 45386``.
DITTO_METER_RE = re.compile(rf"^[{re.escape(DITTO_CHARS)}\s]+(\d{{4,6}})$")


@dataclass
class DittoResult:
    text: str
    #: True when a mark was found and successfully expanded.
    expanded: bool = False
    #: True when a mark was found but there was nothing to inherit from.
    unresolved: bool = False


def looks_like_meter_ditto(text: str | None) -> bool:
    """True when the cell is a repeat mark followed by digits."""
    return bool(DITTO_METER_RE.match(collapse(text)))


def expand_meter_ditto(text: str | None, inherited_prefix: str | None) -> DittoResult:
    """Replace a leading repeat mark with `inherited_prefix`.

    Returns the text unchanged when there is no mark. When a mark is present but
    no prefix has been seen yet, `unresolved` is set so the caller can flag the
    row rather than emit a half-formed code.
    """
    candidate = collapse(text)
    match = DITTO_METER_RE.match(candidate)
    if not match:
        return DittoResult(text=candidate)
    if not inherited_prefix:
        return DittoResult(text=candidate, unresolved=True)
    return DittoResult(text=f"{inherited_prefix}{match.group(1)}", expanded=True)


def expand_remarks_ditto(text: str | None, inherited_remark: str | None) -> DittoResult:
    """Replace a lone repeat mark in the remarks column with the text above it."""
    candidate = collapse(text)
    if not is_ditto(candidate):
        return DittoResult(text=candidate)
    if not inherited_remark:
        return DittoResult(text=candidate, unresolved=True)
    return DittoResult(text=inherited_remark, expanded=True)


class DittoContext:
    """Carries the inherited prefix and remark down a page.

    Both are updated from *resolved* values, so a corrected meter code feeds the
    next ditto rather than the raw OCR reading.
    """

    def __init__(self) -> None:
        self.prefix: str | None = None
        self.remark: str | None = None

    def observe_meter(self, resolved_text: str | None) -> None:
        match = re.match(r"^([A-Z]{2,3})\d{4,6}$", (resolved_text or "").strip().upper())
        if match:
            self.prefix = match.group(1)

    def observe_remark(self, resolved_text: str | None) -> None:
        text = collapse(resolved_text)
        if text and not is_ditto(text):
            self.remark = text
