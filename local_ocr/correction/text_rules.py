"""Rules for the REMARKS column: side codes, non-Latin text, section markers.

The reference workbook's conventions here were reverse-engineered from the 51
remarked rows and cross-checked against the photographs:

* Side codes are written ``+37`` / ``#37`` / ``+128`` on the page but stored as
  ``-37`` / ``-128``. A row carrying only one code stores it as a **number**;
  anything else is text. ``'-37,SHOP'`` and ``'-42,-128'`` are both attested.
* Urdu remarks on the pages are dropped from the workbook entirely.
* ``End Block "C"`` and the ``x - x - x`` divider are structure, not data.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from local_ocr.paths import SECTION_MARKERS

#: Arabic / Urdu / Arabic-presentation-forms code-point ranges. Built from
#: integers so this source file stays pure ASCII and cannot be corrupted by an
#: editor or tool re-encoding it.
NON_LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
)

NON_LATIN_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in NON_LATIN_RANGES) + "]"
)

#: A side code, with or without its leading mark. `allow_bare` decides whether
#: the mark is required -- see `extract_annotations`.
ANNOTATION_RE = re.compile(r"^[+#†]?(\d{1,4})$")

#: Glyphs the surveyor uses as a "same as above" mark, plus the shapes OCR
#: tends to return for them.
DITTO_CHARS = "\"'’“”„″«»×x*%~,·•"

_WHITESPACE_RE = re.compile(r"\s+")


def collapse(text: str | None) -> str:
    """Trim and collapse internal whitespace."""
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def has_non_latin(text: str | None) -> bool:
    return bool(NON_LATIN_RE.search(text or ""))


def strip_non_latin(text: str | None) -> tuple[str, bool]:
    """Remove Urdu/Arabic runs. Returns (remaining text, whether any was found).

    Keeping the English part matters: the ``(4) Shops`` remark on page_10 sits
    directly beneath an Urdu line, and the workbook keeps the English.
    """
    original = text or ""
    if not NON_LATIN_RE.search(original):
        return collapse(original), False
    return collapse(NON_LATIN_RE.sub(" ", original)), True


def is_ditto(text: str | None) -> bool:
    """True when the cell holds nothing but a repeat mark."""
    stripped = collapse(text)
    if not stripped or len(stripped) > 2:
        return False
    return all(ch in DITTO_CHARS for ch in stripped)


def load_section_markers(path: Path | str | None = None) -> list[re.Pattern[str]]:
    path = Path(path) if path is not None else SECTION_MARKERS
    patterns: list[re.Pattern[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def is_section_marker(text: str | None, patterns: list[re.Pattern[str]]) -> bool:
    candidate = collapse(text)
    if not candidate:
        return False
    return any(pattern.match(candidate) for pattern in patterns)


def _circled_to_digit(text: str) -> str:
    """Rewrite circled numerals to plain ones.

    The surveyor circles counts -- page_10 has a circled 4 in ``(4) Shops`` --
    and OCR may return the Unicode circled form.
    """
    out = []
    for char in text:
        name = unicodedata.name(char, "")
        if name.startswith("CIRCLED DIGIT ") or name.startswith("CIRCLED NUMBER "):
            # Circled numerals carry a `digit` property but not a `decimal` one.
            value = unicodedata.digit(char, None)
            out.append(str(value) if value is not None else char)
        else:
            out.append(char)
    return "".join(out)


def extract_annotations(
    text: str | None,
    allow_bare: bool = False,
    sign: str = "-",
) -> tuple[list[str], str]:
    """Split side codes out of a remarks string.

    Returns ``(annotations, residual text)`` with annotations already in output
    form (``+37`` on the page becomes ``-37``).

    `allow_bare` controls whether an unmarked number counts as a side code. It
    must stay False for text coming from the remarks column proper, because
    genuine remarks begin with counts -- ``3 Shops``, ``99 School``, ``4 Shops``
    are all real values that must not be shredded into annotations. The layout
    stage sets it True only for cells it located in the narrow annotation zone
    to the right of the remarks text.
    """
    tokens = collapse(text).split(" ")
    annotations: list[str] = []
    residual: list[str] = []

    for token in tokens:
        if not token:
            continue
        stripped = token.strip(",;")
        match = ANNOTATION_RE.match(stripped)
        marked = bool(stripped) and stripped[0] in "+#†"
        if match and (marked or allow_bare):
            annotations.append(f"{sign}{int(match.group(1))}")
        else:
            residual.append(token)

    return annotations, collapse(" ".join(residual))


def format_remarks(text: str | None, annotations: list[str] | None = None) -> str | int | None:
    """Assemble the REMARKS cell value, matching the reference workbook's types.

    A single side code and nothing else becomes an ``int`` so that the exported
    cell is numeric, exactly as in the source file. Everything else is text,
    comma-joined with side codes first.
    """
    annotations = list(annotations or [])
    body = collapse(_circled_to_digit(text or ""))

    parts = annotations + ([body] if body else [])
    if not parts:
        return None
    if len(parts) == 1 and annotations:
        return int(annotations[0])
    return ",".join(parts)
