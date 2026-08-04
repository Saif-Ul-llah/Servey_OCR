"""Grammar-constrained decoding for the local recognisers.

An unconstrained handwriting model is trained on English prose, so when it is
shown an isolated meter code it produces the nearest *word*: ``SCT45386`` comes
back as ``PRINTEXPORT``. Filtering the output afterwards cannot fix that -- the
letters were never in the image, and the correct reading has already been lost.

The fix is to constrain the decoder while it runs. Two facts about this notebook
make the constraint unusually strong:

* a survey number is **digits only**;
* a meter code is **2-3 letters followed by exactly 5 digits** (only ``LA…``
  carries 6), i.e. an 8-character string over a closed alphabet.

So at every decoding step we mask out every token that cannot continue a legal
string. ``PRINTEXPORT`` becomes unreachable: after three letters the decoder may
only emit digits. The letters that do survive are then snapped to the 65-prefix
whitelist by the existing correction rules, which is a checkable repair rather
than a guess.

This does not make a wrong reading right. It makes a *structurally impossible*
reading unrepresentable, which is what turns unusable output into output the
review grid can work with.
"""

from __future__ import annotations

import numpy as np

from local_ocr.ocr.base import Field

#: Meter codes: how many letters may open a code, and how many digits close it.
METER_MAX_LETTERS = 3
METER_MIN_DIGITS = 5
METER_MAX_DIGITS = 6
#: Survey numbers in the reference data are three digits; allow a little slack.
SURVEY_MAX_DIGITS = 4

_LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DIGITS = set("0123456789")


def token_classes(tokenizer, vocab_size: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Classify every token in the vocabulary as letters-only / digits-only.

    Byte-level BPE tokens carry a leading space marker; it is stripped before
    classification so that ``" 45"`` counts as digits. Returns boolean masks
    plus the decoded text of each token, which the processor uses to know how
    many characters a token would add.
    """
    letters = np.zeros(vocab_size, dtype=bool)
    digits = np.zeros(vocab_size, dtype=bool)
    texts: list[str] = []

    special = set(getattr(tokenizer, "all_special_ids", []) or [])
    for token_id in range(vocab_size):
        raw = tokenizer.convert_ids_to_tokens(token_id)
        text = "" if raw is None else str(raw)
        # 'Ġ' (byte-level BPE) and '▁' (SentencePiece) both stand for a space.
        text = text.replace("Ġ", "").replace("▁", "")
        # Control tokens contribute no characters. Left as their literal spelling
        # they would feed letters like the 's' of "</s>" into the grammar state,
        # which silently corrupts the prefix being tracked.
        if token_id in special or (text.startswith("<") and text.endswith(">")):
            text = ""
        texts.append(text)
        if not text:
            continue
        upper = text.upper()
        if all(char in _LETTERS for char in upper):
            letters[token_id] = True
        elif all(char in _DIGITS for char in upper):
            digits[token_id] = True

    return letters, digits, texts


class FieldGrammarProcessor:
    """A ``LogitsProcessor`` that keeps generation inside the field's grammar.

    Implemented against the transformers ``LogitsProcessor`` protocol without
    importing it, so this module stays importable when torch is absent.
    """

    def __init__(self, field: Field, letters, digits, texts, eos_token_id: int, torch,
                 prefixes: set[str] | None = None):
        self.field = field
        self.letters = torch.as_tensor(letters)
        self.digits = torch.as_tensor(digits)
        self.texts = texts
        self.vocab_size = len(texts)
        self.eos_token_id = eos_token_id
        self.torch = torch

        # Whitelisted prefixes, plus every partial prefix of them, so the trie
        # test is a single set lookup per candidate token.
        self.prefixes = set(prefixes or ())
        self.prefix_set = {
            prefix[:length]
            for prefix in self.prefixes
            for length in range(1, len(prefix) + 1)
        }
        self.prefix_cache: dict[str, object] | None = {} if self.prefixes else None
        self.letter_ids = [i for i, is_letter in enumerate(letters) if is_letter]

        # Masks of "tokens no longer than N characters", for the length cap.
        lengths = [len(text) for text in texts]
        self.max_token_length = max(lengths) if lengths else 1
        cap = min(self.max_token_length, METER_MAX_DIGITS + 1)
        self.by_length = [
            torch.as_tensor(np.array([length <= n for length in lengths], dtype=bool))
            for n in range(cap + 1)
        ]

    # -- state -------------------------------------------------------------

    def _emitted(self, sequence) -> str:
        """Reconstruct the field text generated so far on one beam."""
        out = []
        for token_id in sequence.tolist():
            text = self.texts[token_id] if 0 <= token_id < len(self.texts) else ""
            if text:
                out.append(text)
        return "".join(out).upper()

    def _length_capped(self, base, remaining: int):
        """Restrict `base` to tokens short enough to fit in `remaining` chars.

        Byte-level BPE happily emits ``STRENGTH`` as a single token, which walks
        straight past a "no more than three letters" rule if only the running
        count is checked. The cap has to be applied to the token itself.
        """
        if remaining >= self.max_token_length:
            return base
        index = max(0, min(remaining, len(self.by_length) - 1))
        return base & self.by_length[index]

    def _allowed(self, emitted: str):
        """Mask of tokens that may follow ``emitted``, plus whether EOS is legal."""
        letters = "".join(char for char in emitted if char in _LETTERS)
        digits = "".join(char for char in emitted if char in _DIGITS)
        letter_count, digit_count = len(letters), len(digits)

        if self.field is Field.SURVEY:
            if digit_count >= SURVEY_MAX_DIGITS:
                return None, True
            return self._length_capped(self.digits, SURVEY_MAX_DIGITS - digit_count), digit_count > 0

        # Meter: a whitelisted prefix, then exactly five (sometimes six) digits.
        if digit_count == 0:
            allowed = self._prefix_mask(letters)
            complete = letters in self.prefixes
            if complete:
                # The prefix is whole; the next character may be its first digit.
                allowed = allowed | self._length_capped(self.digits, METER_MAX_DIGITS)
            if allowed is None or not bool(allowed.any()):
                # Nothing legal follows -- let digits close the code rather than
                # dead-ending the beam.
                return self._length_capped(self.digits, METER_MAX_DIGITS), False
            return allowed, False

        remaining = METER_MAX_DIGITS - digit_count
        if digit_count < METER_MIN_DIGITS:
            return self._length_capped(self.digits, remaining), False
        if digit_count < METER_MAX_DIGITS:
            return self._length_capped(self.digits, remaining), True
        return None, True

    def _prefix_mask(self, letters: str):
        """Tokens whose letters keep the code a prefix of a whitelisted code.

        This is the constraint that matters most. Left to choose any letters at
        all, the model reaches for the nearest English word -- ``SCL 93513`` was
        read as ``MEN935131``, with the digits right and the prefix invented.
        Restricting the letter stage to the 65 prefixes the reference workbook
        actually contains turns that into a choice *between real prefixes*,
        decided by the model's own confidence.
        """
        if self.prefix_cache is None:
            return None
        cached = self.prefix_cache.get(letters)
        if cached is not None:
            return cached

        allowed = self.torch.zeros(self.vocab_size, dtype=self.torch.bool)
        for token_id in self.letter_ids:
            candidate = letters + self.texts[token_id].upper()
            if candidate in self.prefix_set:
                allowed[token_id] = True
        self.prefix_cache[letters] = allowed
        return allowed

    # -- LogitsProcessor protocol -----------------------------------------

    def __call__(self, input_ids, scores):
        neg = self.torch.finfo(scores.dtype).min
        for beam in range(scores.shape[0]):
            emitted = self._emitted(input_ids[beam])
            allowed, eos_ok = self._allowed(emitted)

            mask = self.torch.zeros(scores.shape[1], dtype=self.torch.bool, device=scores.device)
            if allowed is not None:
                limit = min(scores.shape[1], allowed.shape[0])
                mask[:limit] = allowed[:limit].to(scores.device)
            if eos_ok and 0 <= self.eos_token_id < mask.shape[0]:
                mask[self.eos_token_id] = True

            if not bool(mask.any()):
                # Never hand the decoder an all-masked row: that yields NaNs.
                continue
            scores[beam] = scores[beam].masked_fill(~mask, neg)
        return scores


def build_processor(tokenizer, field: Field, vocab_size: int, eos_token_id: int, torch):
    """Build a processor for `field`, or None when the field is free text."""
    if field is Field.REMARKS:
        return None

    prefixes: set[str] = set()
    if field is Field.METER:
        try:
            from local_ocr.correction.validator import default_whitelist

            prefixes = {str(prefix).upper() for prefix in default_whitelist()}
        except Exception:  # noqa: BLE001 - a missing whitelist only loosens the constraint
            prefixes = set()

    letters, digits, texts = token_classes(tokenizer, vocab_size)
    return FieldGrammarProcessor(
        field, letters, digits, texts, eos_token_id, torch, prefixes=prefixes
    )
