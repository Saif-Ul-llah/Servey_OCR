"""Data structures shared by every stage of the pipeline.

The pipeline is a sequence of pure-ish transforms over these types:

    image -> list[RowRecord]  (layout + recognition, per page)
           -> list[RowRecord]  (correction, per page)
           -> list[OutputRow]  (assembly, per batch)
           -> workbook         (export)

Nothing is ever dropped silently: a row that cannot be resolved keeps its
``status`` and travels all the way to the review sidecar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Why a value does or does not need a human."""

    OK = "ok"                          # validated and above the auto-accept bar
    CORRECTED = "corrected"            # a rule changed it; still above the bar
    LOW_CONFIDENCE = "low_confidence"  # recognizer was unsure
    DISAGREEMENT = "disagreement"      # ensemble engines returned different strings
    INVALID_PREFIX = "invalid_prefix"  # parses as a meter code, prefix not in whitelist
    INVALID_FORMAT = "invalid_format"  # does not match the meter grammar at all
    AMBIGUOUS = "ambiguous"            # strike-out / arrow correction we refuse to guess
    MANUAL_REVIEW = "manual_review"    # anything else a human must look at
    EMPTY = "empty"                    # no ink in this cell (legitimately blank)
    SKIPPED = "skipped"                # section marker / non-Latin, deliberately not emitted

    @property
    def needs_review(self) -> bool:
        return self not in (Status.OK, Status.CORRECTED, Status.EMPTY, Status.SKIPPED)


class Column(str, Enum):
    SURVEY = "survey"
    METER = "meter"
    REMARKS = "remarks"


@dataclass
class BBox:
    """Axis-aligned box in *rectified* page coordinates."""

    x: int
    y: int
    w: int
    h: int

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class Cell:
    """One recognised region of a page."""

    column: Column
    text: str = ""
    raw_text: str = ""
    confidence: float = 0.0
    bbox: BBox | None = None
    status: Status = Status.OK
    #: Alternative readings, best first. Populated by the ensemble; the review UI
    #: offers these as one-keypress choices, which is where most of the operator
    #: time saving comes from.
    candidates: list[str] = field(default_factory=list)
    #: Names of correction rules that fired, in order. Kept for auditability.
    applied_rules: list[str] = field(default_factory=list)
    #: Per-engine raw output, for the review sidecar and for debugging.
    engine_outputs: dict[str, str] = field(default_factory=dict)

    def note(self, rule: str) -> None:
        if rule not in self.applied_rules:
            self.applied_rules.append(rule)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass
class RowRecord:
    """One handwritten line on one page.

    ``index`` is the line's order on the page, counted from the top, and is the
    stable key used by the golden set and the review sidecar.
    """

    page: str
    index: int
    survey: Cell
    meter: Cell
    remarks: Cell
    #: Index of the bracket span this row belongs to, or None if ungrouped.
    bracket_id: int | None = None
    #: Filled in by grouping: the survey number that governs this row.
    group_survey: str | None = None
    #: True when this row carries the survey number (i.e. starts a group).
    is_group_first: bool = False
    #: Numeric side codes found on this line, as written (e.g. ["+37", "+24"]).
    annotations: list[str] = field(default_factory=list)
    status: Status = Status.OK

    @property
    def needs_review(self) -> bool:
        return (
            self.status.needs_review
            or self.survey.status.needs_review
            or self.meter.status.needs_review
            or self.remarks.status.needs_review
        )

    @property
    def min_confidence(self) -> float:
        scores = [c.confidence for c in (self.survey, self.meter, self.remarks) if not c.is_empty]
        return min(scores) if scores else 0.0


@dataclass
class PageResult:
    """Everything produced from one photograph."""

    path: str
    rows: list[RowRecord] = field(default_factory=list)
    #: Which of the two visible pages we transcribed ("left" or "right").
    page_side: str | None = None
    #: How grouping was resolved on this page: "brackets" or "linear".
    grouping_method: str | None = None
    #: Number of ruled lines detected; used by the row-count audit.
    detected_rules: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def survey_numbers(self) -> list[int]:
        out = []
        for row in self.rows:
            if row.is_group_first and row.group_survey:
                digits = "".join(ch for ch in row.group_survey if ch.isdigit())
                if digits:
                    out.append(int(digits))
        return out


@dataclass
class OutputRow:
    """One row of the deliverable workbook.

    ``survey`` is None on continuation rows -- the reference file leaves those
    cells genuinely empty, not empty-string. ``remarks`` is an int when the line
    carries only side codes, matching the reference file's cell types.
    """

    survey: str | None
    meter: str
    remarks: str | int | None = None
    #: Provenance, carried to the review sidecar rather than the deliverable.
    page: str = ""
    row_index: int = 0
    confidence: float = 1.0
    status: Status = Status.OK


@dataclass
class BatchAudit:
    """Structural checks that catch problems without reading a single glyph."""

    survey_gaps: list[tuple[int, int]] = field(default_factory=list)
    survey_repeats: list[int] = field(default_factory=list)
    duplicate_meters: list[str] = field(default_factory=list)
    row_count_mismatches: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.survey_gaps
            or self.survey_repeats
            or self.duplicate_meters
            or self.row_count_mismatches
        )
