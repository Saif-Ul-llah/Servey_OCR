"""Non-meter placeholder values in the METER# column.

Five such values appear in the reference workbook -- ``NoMeter``,
``MeterMissing``, ``EmptyPlot``, ``Empty``, ``Plot`` -- and the handwriting they
come from varies (``No meter``, ``No metd``, ``no meter``). Matching is fuzzy
because the whole point is to absorb OCR noise on a tiny closed set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz.distance import Levenshtein

from local_ocr.paths import SPECIAL_TOKENS

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def _key(text: str | None) -> str:
    """Lowercase, strip punctuation and whitespace, for distance comparison."""
    return _NORMALISE_RE.sub("", (text or "").lower())


@dataclass
class SpecialToken:
    output: str
    variants: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [_key(v) for v in [*self.variants, self.output]]


class SpecialTokens:
    def __init__(self, tokens: list[SpecialToken], max_distance: int = 2):
        self._tokens = tokens
        self._max_distance = max_distance

    @classmethod
    def load(cls, path: Path | str | None = None) -> "SpecialTokens":
        path = Path(path) if path is not None else SPECIAL_TOKENS
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tokens = [
            SpecialToken(output=item["output"], variants=list(item.get("variants", [])))
            for item in data.get("tokens", [])
        ]
        return cls(tokens, max_distance=int(data.get("max_distance", 2)))

    def match(self, text: str | None) -> tuple[str, int] | None:
        """Nearest placeholder value and its edit distance, or None.

        Longer variants get a proportionally larger budget: ``Demolished`` should
        never collapse into ``Empty``, but ``no metd`` must still reach
        ``no meter``.
        """
        candidate = _key(text)
        if not candidate:
            return None

        best: tuple[str, int] | None = None
        for token in self._tokens:
            for key in token.keys:
                budget = min(self._max_distance, max(1, len(key) // 3))
                distance = Levenshtein.distance(candidate, key, score_cutoff=budget)
                if distance <= budget and (best is None or distance < best[1]):
                    best = (token.output, distance)
        return best

    @property
    def outputs(self) -> list[str]:
        return [t.output for t in self._tokens]
