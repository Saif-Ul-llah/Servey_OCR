"""Position-aware repair of garbled meter codes.

A meter code is ``[A-Z]{2,3}`` followed by exactly 5 digits, so *position
determines character type*. That single fact removes the whole ``S/5``,
``O/0``, ``I/1``, ``G/6`` confusion class by construction: a ``5`` sitting in a
letter slot can only ever have been an ``S``.

**Digit-to-digit substitution is deliberately not performed here.** With no
master list of valid meter numbers, nothing can confirm that turning a ``3``
into an ``8`` produced the right answer -- it would just manufacture a
plausible, unverifiable, silent error. Only *type* fixes (letter-in-digit-slot,
digit-in-letter-slot) and prefix repair are applied, because both are checkable:
the grammar validates the former, the closed prefix whitelist the latter.
Digit-level alternatives are surfaced to the review UI and to within-group
sequence repair, which have independent evidence; they never auto-apply.
"""

from __future__ import annotations

from dataclasses import dataclass

from local_ocr.correction.validator import (
    LAYOUTS,
    PrefixWhitelist,
    default_whitelist,
    normalise,
)

#: A digit glyph found where a letter must be. Ordered most-likely first.
DIGIT_TO_LETTER: dict[str, tuple[str, ...]] = {
    "0": ("O", "D", "Q", "U"),
    "1": ("I", "L", "T", "J"),
    "2": ("Z", "R"),
    "3": ("B", "E"),
    "4": ("A", "Y", "U"),
    "5": ("S",),
    "6": ("G", "C", "L"),
    "7": ("T", "I", "F", "J", "Y"),
    "8": ("B", "S"),
    "9": ("G", "P", "J", "Y", "A"),
}

#: A letter glyph found where a digit must be.
LETTER_TO_DIGIT: dict[str, tuple[str, ...]] = {
    "O": ("0",), "D": ("0",), "Q": ("0",), "U": ("0", "4"),
    "I": ("1",), "L": ("1",), "T": ("7", "1"), "J": ("1", "7"),
    "Z": ("2",), "R": ("2",),
    "B": ("8", "3"), "E": ("3",),
    "A": ("4", "9"), "V": ("4",), "Y": ("4", "7", "9"),
    "S": ("5",), "G": ("6", "9"), "C": ("6", "0"),
    "F": ("7",), "P": ("9",),
}

#: Letter-for-letter confusions seen in this hand. ``SCT`` vs ``SCI`` vs ``SCJ``
#: and ``SFE`` vs ``SFF`` are the two that actually recur in the sample pages.
LETTER_TO_LETTER: dict[str, tuple[str, ...]] = {
    "T": ("I", "J", "F", "Y"),
    "I": ("T", "J", "L"),
    "J": ("T", "I", "Y"),
    "E": ("F", "C", "L"),
    "F": ("E", "P", "T"),
    "C": ("G", "O", "E", "L"),
    "G": ("C", "O", "Q", "S"),
    "O": ("D", "Q", "C", "U"),
    "D": ("O", "P", "B"),
    "U": ("V", "O", "N", "W"),
    "V": ("U", "Y"),
    "S": ("G", "Z"),
    "A": ("H", "R", "U"),
    "P": ("F", "R", "D"),
    "L": ("I", "C", "E"),
    "Y": ("T", "V", "J"),
    "H": ("A", "N", "K"),
    "M": ("N", "W"),
    "N": ("M", "H", "U"),
    "B": ("R", "D", "P"),
    "R": ("P", "B", "K"),
    "K": ("R", "H"),
    "X": ("K", "Y"),
    "W": ("M", "V"),
    "Q": ("O", "G"),
    "Z": ("S", "Q"),
}

#: Fuzzy prefix matching is only trusted for 3-letter prefixes. Two characters
#: carry too little signal -- almost any 2-letter string scores highly against
#: some whitelist entry, so a 2-letter prefix must match exactly.
MIN_FUZZY_PREFIX_LEN = 3


@dataclass(frozen=True)
class MeterCandidate:
    """A repaired reading of a meter code."""

    text: str
    prefix: str
    digits: str
    #: Number of characters changed from the OCR output.
    edits: int
    #: 100.0 when the prefix was already on the whitelist, else the fuzzy score.
    prefix_score: float
    #: True when no character was changed and the prefix was already known.
    exact: bool

    @property
    def rank_key(self) -> tuple:
        # Fewest edits first, then best prefix score.
        return (self.edits, -self.prefix_score)


def _letter_options(char: str) -> list[tuple[str, int]]:
    """Candidate letters for a glyph occupying a letter slot, with edit cost."""
    if char.isalpha():
        options = [(char, 0)]
        options += [(alt, 1) for alt in LETTER_TO_LETTER.get(char, ())]
        return options
    return [(alt, 1) for alt in DIGIT_TO_LETTER.get(char, ())]


def _digit_options(char: str) -> list[tuple[str, int]]:
    """Candidate digits for a glyph occupying a digit slot, with edit cost.

    A digit that is already a digit has exactly one option: itself. See the
    module docstring for why no digit-to-digit substitution happens here.
    """
    if char.isdigit():
        return [(char, 0)]
    return [(alt, 1) for alt in LETTER_TO_DIGIT.get(char, ())]


def _beam(chars: str, options_for, max_edits: int, beam_width: int = 24) -> list[tuple[str, int]]:
    """Enumerate low-cost rewritings of `chars`, cheapest first."""
    beam: list[tuple[str, int]] = [("", 0)]
    for char in chars:
        options = options_for(char)
        if not options:
            return []
        nxt: list[tuple[str, int]] = []
        for text, cost in beam:
            for alt, alt_cost in options:
                total = cost + alt_cost
                if total <= max_edits:
                    nxt.append((text + alt, total))
        if not nxt:
            return []
        nxt.sort(key=lambda tc: tc[1])
        beam = nxt[:beam_width]
    return beam


def repair_meter(
    raw: str,
    whitelist: PrefixWhitelist | None = None,
    max_edits: int = 2,
    fuzzy_threshold: int = 85,
    limit: int = 5,
) -> list[MeterCandidate]:
    """Return plausible meter codes for a raw OCR reading, best first.

    An empty list means the reading could not be forced into the grammar at all
    -- the caller should flag it ``INVALID_FORMAT`` rather than guess.
    """
    whitelist = whitelist if whitelist is not None else default_whitelist()
    clean = normalise(raw)
    if not clean:
        return []

    seen: dict[str, MeterCandidate] = {}

    for n_letters, n_digits in LAYOUTS:
        if len(clean) != n_letters + n_digits:
            continue

        digit_beam = _beam(clean[n_letters:], _digit_options, max_edits)
        if not digit_beam:
            continue
        letter_beam = _beam(clean[:n_letters], _letter_options, max_edits)
        if not letter_beam:
            continue

        for prefix, prefix_cost in letter_beam:
            # Two letters carry too little signal to rewrite. Allowing
            # substitutions there lets a dropped character in a 3-letter prefix
            # ("SF_ 77430") land on a one-off whitelist entry like "SE" and look
            # confident. A 2-letter prefix must therefore be read exactly.
            if n_letters < MIN_FUZZY_PREFIX_LEN and prefix_cost > 0:
                continue

            if prefix in whitelist:
                resolved, score = prefix, 100.0
            elif n_letters >= MIN_FUZZY_PREFIX_LEN:
                match = whitelist.best_match(prefix, threshold=fuzzy_threshold)
                if match is None:
                    continue
                resolved, score = match
                # A fuzzy hit is itself an edit; charge for it so that a
                # zero-edit exact prefix always outranks a rewritten one.
                prefix_cost += 1
            else:
                continue

            for digits, digit_cost in digit_beam:
                total = prefix_cost + digit_cost
                if total > max_edits:
                    continue
                text = f"{resolved}{digits}"
                candidate = MeterCandidate(
                    text=text,
                    prefix=resolved,
                    digits=digits,
                    edits=total,
                    prefix_score=score,
                    exact=(total == 0),
                )
                current = seen.get(text)
                if current is None or candidate.rank_key < current.rank_key:
                    seen[text] = candidate

    ranked = sorted(seen.values(), key=lambda c: c.rank_key)
    return ranked[:limit]


def coerce_digits(text: str) -> str:
    """Rewrite letter glyphs as digits in a field that can only hold digits.

    Used for the survey-number column, where the grammar admits nothing else, so
    a letter can only ever be a misread digit. Whitespace and existing digits are
    left alone; unmappable characters are dropped rather than guessed at.
    """
    out: list[str] = []
    for char in (text or "").upper():
        if char.isdigit() or char.isspace():
            out.append(char)
        elif char in LETTER_TO_DIGIT:
            out.append(LETTER_TO_DIGIT[char][0])
        else:
            out.append(" ")
    return "".join(out)


def digit_alternatives(digits: str, position: int) -> list[str]:
    """Digit strings one confusion apart from `digits` at `position`.

    Used by the review UI and by within-group sequence repair -- never applied
    automatically.
    """
    confusions = {
        "0": "68", "1": "7", "2": "7", "3": "89", "4": "9",
        "5": "68", "6": "058", "7": "129", "8": "3506", "9": "478",
    }
    char = digits[position]
    return [digits[:position] + alt + digits[position + 1:] for alt in confusions.get(char, "")]
