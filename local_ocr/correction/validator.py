"""Meter-code grammar and the prefix whitelist.

Two facts about the real data drive everything here, both verified against all
881 rows of the reference workbook:

* the digit part is **exactly 5 digits** (857 of 862 parsable codes; the only
  6-digit ones carry the ``LA`` prefix), and
* there are exactly **65 distinct prefixes**, all known in advance.

Together they make a meter code an 8-character string drawn from a closed set of
prefixes plus a fixed-width number -- which is a far tighter constraint than
generic OCR post-processing, and is what makes high accuracy reachable on
low-resolution handwriting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from local_ocr.paths import PREFIX_WHITELIST

#: Canonical grammar. 2-3 uppercase letters followed by exactly 5 digits.
METER_RE = re.compile(r"^([A-Z]{2,3})(\d{5})$")
#: Relaxed grammar admitting the 6-digit ``LA`` codes.
METER_RE_RELAXED = re.compile(r"^([A-Z]{2,3})(\d{5,6})$")

#: Layouts tried when repairing a code, as (letter count, digit count).
#: Ordered by prior probability, so 3+5 is preferred over 2+6 for a length-8
#: string -- 857 observations against 5.
LAYOUTS: tuple[tuple[int, int], ...] = ((3, 5), (2, 5), (3, 6), (2, 6))


@dataclass(frozen=True)
class Meter:
    prefix: str
    digits: str

    def __str__(self) -> str:
        return f"{self.prefix}{self.digits}"


class PrefixWhitelist:
    """The closed set of meter prefixes, with observation counts.

    Counts break ties in fuzzy matching: given two equally-scoring candidates,
    the one seen more often in the reference data wins. Several whitelist
    entries are singletons that look like transcription slips in the source
    workbook (``SC``, ``SE``, ``SP``, ``SY``); keeping them means we never
    reject a value the existing workflow considers legitimate, while the counts
    stop them from stealing matches from real prefixes.
    """

    def __init__(self, counts: dict[str, int]):
        self._counts = dict(counts)
        self._choices = list(self._counts)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "PrefixWhitelist":
        path = Path(path) if path is not None else PREFIX_WHITELIST
        counts: dict[str, int] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            prefix, _, count = line.partition("\t")
            prefix = prefix.strip().upper()
            if prefix:
                counts[prefix] = int(count.strip() or 0)
        if not counts:
            raise ValueError(f"prefix whitelist is empty: {path}")
        return cls(counts)

    def __contains__(self, prefix: object) -> bool:
        return isinstance(prefix, str) and prefix.upper() in self._counts

    def __len__(self) -> int:
        return len(self._counts)

    def __iter__(self):
        return iter(self._choices)

    def count(self, prefix: str) -> int:
        return self._counts.get(prefix.upper(), 0)

    def best_match(self, candidate: str, threshold: int = 85) -> tuple[str, float] | None:
        """Nearest whitelist prefix, or None if nothing scores above `threshold`.

        Ties are broken by observation count, so a garbled prefix resolves to
        the common ``SCO`` rather than the one-off ``SC``.
        """
        candidate = candidate.strip().upper()
        if not candidate:
            return None
        if candidate in self._counts:
            return candidate, 100.0

        matches = process.extract(
            candidate, self._choices, scorer=fuzz.ratio, limit=5, score_cutoff=threshold
        )
        if not matches:
            return None
        best_score = matches[0][1]
        tied = [m for m in matches if m[1] == best_score]
        winner = max(tied, key=lambda m: self._counts[m[0]])
        return winner[0], float(best_score)


@lru_cache(maxsize=1)
def default_whitelist() -> PrefixWhitelist:
    """Process-wide whitelist, loaded once from config/meter_prefixes.txt."""
    return PrefixWhitelist.load()


def normalise(text: str) -> str:
    """Strip everything that cannot appear in a meter code and uppercase.

    Handwritten codes are written with a space between prefix and digits, and
    OCR routinely inserts stray punctuation, so this is the entry point for
    every meter-code operation.
    """
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def parse_meter(text: str, relaxed: bool = True) -> Meter | None:
    """Split a meter code into prefix and digits, or return None if it is not one.

    Does not consult the whitelist -- this is a pure grammar check.
    """
    pattern = METER_RE_RELAXED if relaxed else METER_RE
    match = pattern.match(normalise(text))
    if not match:
        return None
    return Meter(match.group(1), match.group(2))


def is_valid_meter(
    text: str,
    whitelist: PrefixWhitelist | None = None,
    relaxed: bool = True,
) -> bool:
    """True when `text` parses as a meter code *and* its prefix is known."""
    meter = parse_meter(text, relaxed=relaxed)
    if meter is None:
        return False
    whitelist = whitelist if whitelist is not None else default_whitelist()
    return meter.prefix in whitelist
