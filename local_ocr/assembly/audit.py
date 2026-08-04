"""Structural checks that find problems without reading a single glyph.

These are the cheapest accuracy tools in the project. Survey numbers run
consecutively through the notebook and every meter code is unique, so a gap, a
repeat or a duplicate is proof that something went wrong -- a page was missed,
a page was photographed twice, or a number was misread -- and it is detectable
with no reference to the image at all.

Across all 881 rows of the reference workbook the survey sequence has exactly
one genuine gap (325 -> 336), so `known_gaps` exists to record surveyor-side
gaps that are not our errors.
"""

from __future__ import annotations

import re
from collections import Counter

from local_ocr.models import BatchAudit, OutputRow, PageResult

#: Values in the METER# column that are placeholders, not meter numbers, and so
#: are allowed to repeat.
PLACEHOLDERS = {"NoMeter", "MeterMissing", "EmptyPlot", "Empty", "Plot"}

_SURVEY_DIGITS_RE = re.compile(r"\d+")


def survey_number(value: str | None) -> int | None:
    match = _SURVEY_DIGITS_RE.search(value or "")
    return int(match.group()) if match else None


def audit(
    rows: list[OutputRow],
    pages: list[PageResult] | None = None,
    known_gaps: set[tuple[int, int]] | None = None,
) -> BatchAudit:
    """Run every structural check over an assembled batch."""
    known_gaps = known_gaps or set()
    result = BatchAudit()

    numbers = [n for n in (survey_number(r.survey) for r in rows) if n is not None]

    for previous, current in zip(numbers, numbers[1:]):
        if current == previous + 1:
            continue
        if current <= previous:
            result.survey_repeats.append(current)
        elif (previous, current) not in known_gaps:
            result.survey_gaps.append((previous, current))

    counts = Counter(r.meter for r in rows if r.meter and r.meter not in PLACEHOLDERS)
    result.duplicate_meters = sorted(meter for meter, n in counts.items() if n > 1)

    for page in pages or []:
        if not page.detected_rules:
            continue
        emitted = sum(1 for row in page.rows if not row.meter.is_empty)
        if emitted != page.detected_rules:
            result.row_count_mismatches.append((page.path, page.detected_rules, emitted))

    return result


def describe(result: BatchAudit) -> list[str]:
    """Human-readable lines for the log and the Summary of a run."""
    lines: list[str] = []
    for previous, current in result.survey_gaps:
        missing = current - previous - 1
        lines.append(
            f"survey gap: KE-{previous} -> KE-{current} "
            f"({missing} number{'s' if missing != 1 else ''} missing; a page may not have been photographed)"
        )
    for number in result.survey_repeats:
        lines.append(f"survey out of order or repeated at KE-{number}")
    for meter in result.duplicate_meters:
        lines.append(f"duplicate meter code: {meter} (a misread, or a page photographed twice)")
    for path, expected, got in result.row_count_mismatches:
        lines.append(f"row count mismatch in {path}: {expected} ruled lines with ink, {got} rows emitted")
    if not lines:
        lines.append("structural audit clean: surveys consecutive, meter codes unique, row counts match")
    return lines
