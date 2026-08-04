"""Apply every correction rule to one page's rows, in the order they must run.

Order matters. Section markers are removed before anything tries to parse them;
repeat marks are expanded before validation, because ``" 45386`` is not a meter
code until its prefix is inherited; and the inherited prefix is taken from the
*resolved* value of the line above, so a corrected code feeds the next ditto.

Every rule that fires is recorded on the cell (``applied_rules``) and every
uncertain outcome sets a status that survives all the way to the review
sidecar. Nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from local_ocr.correction.char_confusion import coerce_digits, repair_meter
from local_ocr.correction.ditto import (
    DittoContext,
    expand_meter_ditto,
    expand_remarks_ditto,
    looks_like_meter_ditto,
)
from local_ocr.correction.glyphs import DIGIT_RUN_RE, resolve_corrected_digits
from local_ocr.correction.text_rules import (
    collapse,
    extract_annotations,
    is_ditto,
    is_section_marker,
    load_section_markers,
    strip_non_latin,
)
from local_ocr.correction.tokens import SpecialTokens
from local_ocr.correction.validator import (
    PrefixWhitelist,
    default_whitelist,
    normalise,
    parse_meter,
)
from local_ocr.models import Cell, RowRecord, Status

#: Survey numbers in the reference data run 324..683, i.e. three digits.
SURVEY_RUN_RE = re.compile(r"\d{2,4}")


@dataclass
class CorrectionConfig:
    survey_prefix: str = "KE-"
    fuzzy_prefix_threshold: int = 85
    max_edits: int = 2
    drop_non_latin_remarks: bool = True
    annotation_sign: str = "-"


class Corrector:
    """Holds the loaded whitelist / token table / marker list.

    Constructing one is cheap after the first time -- the whitelist is cached
    process-wide -- so a worker process can make its own.
    """

    def __init__(
        self,
        whitelist: PrefixWhitelist | None = None,
        tokens: SpecialTokens | None = None,
        markers: list[re.Pattern[str]] | None = None,
        config: CorrectionConfig | None = None,
    ):
        self.whitelist = whitelist if whitelist is not None else default_whitelist()
        self.tokens = tokens if tokens is not None else SpecialTokens.load()
        self.markers = markers if markers is not None else load_section_markers()
        self.config = config or CorrectionConfig()

    # ---------------------------------------------------------------- survey

    def correct_survey(self, cell: Cell) -> Cell:
        """Turn a survey-number cell into ``KE-nnn``.

        Crossed-out corrections happen here too -- page_09 has ``662 663`` and
        page_10 has ``678 668``, both struck-and-rewritten -- so the same
        last-complete-run rule as meter codes applies.
        """
        raw = collapse(cell.raw_text or cell.text)
        cell.raw_text = cell.raw_text or raw

        if not raw:
            cell.text = ""
            cell.status = Status.EMPTY
            return cell

        digits_source = coerce_digits(raw)
        runs = SURVEY_RUN_RE.findall(digits_source)

        if not runs:
            cell.text = ""
            cell.status = Status.INVALID_FORMAT
            cell.note("survey_no_digits")
            return cell

        if len(runs) > 1:
            cell.note("survey_struck_out")
            cell.candidates = [f"{self.config.survey_prefix}{r}" for r in dict.fromkeys(runs)]

        # The surviving value is the last one written.
        number = runs[-1]
        cell.text = f"{self.config.survey_prefix}{number}"
        if digits_source != raw.upper():
            cell.note("survey_char_confusion")
            cell.status = Status.CORRECTED
        elif len(runs) > 1:
            cell.status = Status.CORRECTED
        return cell

    # ----------------------------------------------------------------- meter

    def correct_meter(self, cell: Cell, ctx: DittoContext) -> Cell:
        raw = collapse(cell.raw_text or cell.text)
        cell.raw_text = cell.raw_text or raw

        if not raw:
            cell.text = ""
            cell.status = Status.EMPTY
            return cell

        if is_section_marker(raw, self.markers):
            cell.text = ""
            cell.status = Status.SKIPPED
            cell.note("section_marker")
            return cell

        # Placeholder values ("No meter", "Vacant Plot") never reach the grammar.
        token = self.tokens.match(raw)
        if token is not None and not DIGIT_RUN_RE.search(raw):
            cell.text, distance = token
            cell.status = Status.OK if distance == 0 else Status.CORRECTED
            cell.note("special_token")
            return cell

        candidate = raw
        prefix_hint: str | None = None

        # 1. Repeat mark -- inherit the prefix from the line physically above.
        if looks_like_meter_ditto(candidate):
            ditto = expand_meter_ditto(candidate, ctx.prefix)
            if ditto.unresolved:
                cell.text = candidate
                cell.status = Status.MANUAL_REVIEW
                cell.note("ditto_no_prefix")
                return cell
            candidate = ditto.text
            cell.note("ditto_meter")

        # 2. Correction marks -- strike-out or arrow. Multiple digit runs only.
        resolution = resolve_corrected_digits(candidate)
        if resolution.ambiguous:
            cell.text = candidate
            cell.status = Status.AMBIGUOUS
            cell.candidates = resolution.candidates
            cell.note("ambiguous_correction")
            return cell
        if resolution.corrected and resolution.digits:
            head = candidate[: candidate.index(resolution.candidates[0])] if resolution.candidates else ""
            prefix_hint = normalise(DIGIT_RUN_RE.sub("", head))[-3:] or None
            if prefix_hint is None and ctx.prefix:
                prefix_hint = ctx.prefix
            candidate = f"{prefix_hint or ''}{resolution.digits}"
            cell.note("struck_out_correction")

        # 3. Grammar + whitelist repair.
        candidates = repair_meter(
            candidate,
            whitelist=self.whitelist,
            max_edits=self.config.max_edits,
            fuzzy_threshold=self.config.fuzzy_prefix_threshold,
        )
        if not candidates:
            cell.text = normalise(candidate)
            cell.status = (
                Status.INVALID_PREFIX
                if parse_meter(candidate) is not None
                else Status.INVALID_FORMAT
            )
            cell.note("repair_failed")
            return cell

        best = candidates[0]
        cell.text = best.text
        cell.candidates = [c.text for c in candidates[1:]]
        if not best.exact:
            cell.note(f"repair_{best.edits}_edit")
        # A rule fired somewhere along the way (ditto, strike-out, repair), so
        # the value is no longer the raw reading and is marked as such.
        cell.status = Status.CORRECTED if cell.applied_rules else Status.OK
        return cell

    # --------------------------------------------------------------- remarks

    def correct_remarks(self, cell: Cell, ctx: DittoContext) -> tuple[Cell, list[str]]:
        """Clean a remarks cell. Returns the cell and any side codes found on it."""
        raw = collapse(cell.raw_text or cell.text)
        cell.raw_text = cell.raw_text or raw

        if not raw:
            cell.text = ""
            cell.status = Status.EMPTY
            return cell, []

        if is_section_marker(raw, self.markers):
            cell.text = ""
            cell.status = Status.SKIPPED
            cell.note("section_marker")
            return cell, []

        candidate = raw
        if self.config.drop_non_latin_remarks:
            candidate, found = strip_non_latin(candidate)
            if found:
                cell.note("non_latin_dropped")
                if not candidate:
                    cell.text = ""
                    cell.status = Status.SKIPPED
                    return cell, []

        if is_ditto(candidate):
            ditto = expand_remarks_ditto(candidate, ctx.remark)
            if ditto.unresolved:
                cell.text = candidate
                cell.status = Status.MANUAL_REVIEW
                cell.note("ditto_no_remark")
                return cell, []
            candidate = ditto.text
            cell.note("ditto_remark")
            cell.status = Status.CORRECTED

        annotations, residual = extract_annotations(
            candidate, allow_bare=False, sign=self.config.annotation_sign
        )
        if annotations:
            cell.note("annotation_extracted")

        cell.text = residual
        return cell, annotations

    # ------------------------------------------------------------------ page

    def correct_page(self, rows: list[RowRecord]) -> list[RowRecord]:
        """Run every rule over one page, top to bottom.

        The `DittoContext` deliberately spans the whole page rather than
        resetting per survey group: on page_09 the repeat mark on KE-662's line
        inherits from KE-661's meter.
        """
        ctx = DittoContext()

        for row in rows:
            self.correct_survey(row.survey)
            self.correct_meter(row.meter, ctx)
            _, annotations = self.correct_remarks(row.remarks, ctx)

            # Side codes may also arrive from a dedicated annotation-zone cell,
            # in which case the layout stage has already put them on the row.
            for code in annotations:
                if code not in row.annotations:
                    row.annotations.append(code)

            ctx.observe_meter(row.meter.text)
            ctx.observe_remark(row.remarks.text)

            row.is_group_first = bool(row.survey.text)
            if row.survey.text:
                row.group_survey = row.survey.text

            if row.meter.status is Status.SKIPPED and row.remarks.status in (
                Status.SKIPPED,
                Status.EMPTY,
            ):
                row.status = Status.SKIPPED

        return rows
