"""Struck-out and arrow-confirmed meter codes.

The surveyor corrects a mistake in one of two ways, both present in the samples:

* **Arrow confirmation** -- ``SCE 10194 <- 10194`` on page_07. The arrow points
  at the value that stands.
* **Strike-and-rewrite** -- ``SFL 836 83628`` on page_09 and
  ``SCO 813(7)6 | 81376`` on page_01. The struck digits are followed by the
  correct ones.

Both reduce to the same rule: **take the last complete digit run on the line**.
That is safe because a struck-out fragment is, by construction, written before
its replacement, and an arrow points rightwards to the surviving value.

The rule refuses to fire when two *complete and different* runs are present --
there is no way to tell a correction from two meters crammed onto one line, so
the row is flagged ``AMBIGUOUS`` for a human instead of guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from local_ocr.correction.text_rules import collapse

#: Arrows and equals signs the surveyor uses to point at the surviving value.
POINTER_CHARS = "←→=>➜⟶"
POINTER_RE = re.compile(rf"[{re.escape(POINTER_CHARS)}]|<-|->|--+>")

#: Any run of digits on the line.
DIGIT_RUN_RE = re.compile(r"\d+")

#: The canonical digit-run length; see validator.METER_RE.
FULL_RUN = 5


@dataclass
class GlyphResolution:
    """Outcome of inspecting a meter cell for correction marks."""

    digits: str | None
    #: True when a correction mark was detected at all.
    corrected: bool = False
    #: True when we declined to choose and the row needs a human.
    ambiguous: bool = False
    #: Every complete digit run seen, in written order. Offered to the review UI.
    candidates: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = []


def has_pointer(text: str | None) -> bool:
    return bool(POINTER_RE.search(text or ""))


def resolve_corrected_digits(text: str | None, full_run: int = FULL_RUN) -> GlyphResolution:
    """Pick the surviving digit run from a meter cell that carries a correction.

    Returns ``digits=None`` when the cell holds a single run and therefore needs
    no resolution -- the caller should carry on with its normal path.
    """
    candidate = collapse(text)
    runs = DIGIT_RUN_RE.findall(candidate)
    complete = [run for run in runs if len(run) >= full_run]

    if len(runs) <= 1:
        return GlyphResolution(digits=None, candidates=complete)

    distinct = list(dict.fromkeys(complete))

    if len(distinct) > 1:
        # Two complete, different runs: a correction, or two meters on one line.
        # Both readings are plausible, so refuse to choose.
        return GlyphResolution(digits=None, ambiguous=True, candidates=distinct)

    if distinct:
        # One complete run, repeated or preceded by a struck-out fragment.
        return GlyphResolution(digits=distinct[0], corrected=True, candidates=distinct)

    # Several fragments, none complete -- nothing trustworthy to salvage.
    return GlyphResolution(digits=None, ambiguous=True, candidates=runs)


def strip_pointer_section(text: str | None) -> str:
    """Drop everything from the first pointer onwards.

    Used when the pointed-to value has already been resolved and the prefix must
    be read from the left-hand side alone.
    """
    candidate = collapse(text)
    match = POINTER_RE.search(candidate)
    return collapse(candidate[: match.start()]) if match else candidate
