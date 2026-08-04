"""Turn per-page results into the batch of rows the workbook will hold.

Three things can only be decided at batch level, which is why this stage exists
rather than each page exporting itself:

* **Groups straddle pages.** KE-619 and KE-673 both start on one photograph and
  continue on the next, with the surveyor rewriting the number at the top of the
  continuing page. The workbook labels such a group once, on its first row.
* **Remarks bind across pages.** The ``3 Shops`` note written on page_11 belongs
  to KE-673, whose first row is on page_10.
* **The structural audits are batch-wide** -- survey continuity and meter
  uniqueness mean nothing within a single page.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from local_ocr.assembly.audit import audit, survey_number
from local_ocr.correction.text_rules import format_remarks
from local_ocr.models import BatchAudit, OutputRow, PageResult, RowRecord, Status


@dataclass
class AssemblyConfig:
    #: Free-text remarks attach to the row bearing the survey number.
    bind_text_remarks_to_group: bool = True
    #: Joiner used when a group carries remark text on more than one line.
    #: The surveyor's own joiner varies (", " and " + " both appear in the
    #: reference workbook), so any joined remark is flagged for a human.
    remark_joiner: str = ", "
    flag_joined_remarks: bool = True
    known_survey_gaps: set[tuple[int, int]] = field(default_factory=set)


@dataclass
class Batch:
    rows: list[OutputRow]
    audit: BatchAudit
    #: Records that produced no output row, with the reason. Nothing vanishes.
    skipped: list[tuple[RowRecord, str]] = field(default_factory=list)


def order_pages(pages: list[PageResult]) -> list[PageResult]:
    """Sort pages by the first survey number they show.

    Filenames carry no reliable order -- the sample set is named by WhatsApp
    timestamp and runs backwards through the notebook. Pages with no survey
    number at all keep their input position, appended at the end.
    """
    numbered: list[tuple[int, PageResult]] = []
    unnumbered: list[PageResult] = []
    for page in pages:
        numbers = page.survey_numbers
        (numbered.append((numbers[0], page)) if numbers else unnumbered.append(page))
    numbered.sort(key=lambda pair: pair[0])
    return [page for _, page in numbered] + unnumbered


def _flatten(pages: list[PageResult]) -> tuple[list[RowRecord], list[tuple[RowRecord, str]]]:
    rows: list[RowRecord] = []
    skipped: list[tuple[RowRecord, str]] = []
    for page in pages:
        for row in page.rows:
            if row.status is Status.SKIPPED:
                skipped.append((row, "section marker or non-Latin line"))
            elif row.meter.is_empty and row.remarks.is_empty and not row.survey.text:
                skipped.append((row, "no ink in any column"))
            else:
                rows.append(row)
    return rows, skipped


def assign_groups(rows: list[RowRecord]) -> list[int]:
    """Give every row a group index, healing groups that straddle a page break.

    When a page opens with the same survey number the previous page ended on,
    the surveyor is continuing a group rather than starting one, so the repeat
    is demoted to a continuation row and no second ``KE-nnn`` is emitted.
    """
    group_ids: list[int] = []
    group_id = -1
    current_survey: str | None = None

    for row in rows:
        if row.is_group_first and row.group_survey:
            if row.group_survey == current_survey:
                row.is_group_first = False
                row.survey.note("group_continues_across_pages")
            else:
                group_id += 1
                current_survey = row.group_survey

        if group_id < 0:
            # The batch opens mid-group: the page holding the survey number was
            # not photographed. Emit the rows, but let the audit see the hole.
            group_id = 0
            current_survey = current_survey or None

        row.group_survey = current_survey
        group_ids.append(group_id)

    return group_ids


def bind_remarks(
    rows: list[RowRecord],
    group_ids: list[int],
    config: AssemblyConfig,
) -> dict[int, str]:
    """Collect each group's plot-level remark(s) onto its first row.

    A remark binds to the group only when it stands alone. When it shares its
    line with a side code it stays put, because the pair describes that one
    meter rather than the plot. Both behaviours are attested:

    * ``Shop`` beside the third meter of KE-646 is recorded on KE-646's first
      row (workbook row 770), and ``2 Shops`` beside the second meter of KE-676
      on its first (row 848) -- standalone text, bound to the group;
    * ``+37 Shop`` beside SCO76794, a *continuation* row of KE-665, is recorded
      on that row as ``'-37,SHOP'`` (row 811) -- text sharing a line with a side
      code, left where it was written.

    A single handwritten line can hold two remarks separated by a wide gap --
    ``Shop`` and ``Hair cutting saloon`` on page_01 become
    ``'Shop, Hair Cutting Saloon'`` in the workbook. The layout stage splits
    those into separate segments and joins them here, so the same joiner and the
    same review flag cover both cases.
    """
    if not config.bind_text_remarks_to_group:
        return {}

    per_group: dict[int, list[str]] = {}
    for row, group_id in zip(rows, group_ids):
        if row.annotations:
            continue
        text = row.remarks.text.strip()
        if text:
            per_group.setdefault(group_id, []).append(text)

    bound: dict[int, str] = {}
    for group_id, parts in per_group.items():
        bound[group_id] = config.remark_joiner.join(parts)
        if len(parts) > 1 and config.flag_joined_remarks:
            first = next(r for r, g in zip(rows, group_ids) if g == group_id)
            first.remarks.status = Status.MANUAL_REVIEW
            first.remarks.note("remarks_joined")
            first.remarks.candidates = parts
    return bound


def _row_status(row: RowRecord) -> Status:
    """Collapse the three cell statuses into one verdict for the row."""
    if row.needs_review:
        return Status.MANUAL_REVIEW
    cells = (row.survey, row.meter, row.remarks)
    return Status.CORRECTED if any(c.status is Status.CORRECTED for c in cells) else Status.OK


def assemble(pages: list[PageResult], config: AssemblyConfig | None = None) -> Batch:
    """Merge pages into the ordered row list the exporter writes."""
    config = config or AssemblyConfig()

    ordered = order_pages(pages)
    rows, skipped = _flatten(ordered)
    group_ids = assign_groups(rows)
    group_remarks = bind_remarks(rows, group_ids, config)

    output: list[OutputRow] = []
    for row, group_id in zip(rows, group_ids):
        # Side codes always stay on the line they were written beside, and take
        # any text sharing that line with them. Standalone text has been
        # gathered onto the group's first row.
        if row.annotations:
            text = row.remarks.text.strip()
        elif row.is_group_first:
            text = group_remarks.get(group_id, "")
        else:
            text = ""
        output.append(
            OutputRow(
                survey=row.group_survey if row.is_group_first else None,
                meter=row.meter.text,
                remarks=format_remarks(text, row.annotations),
                page=row.page,
                row_index=row.index,
                confidence=row.min_confidence,
                status=_row_status(row),
            )
        )

    return Batch(
        rows=output,
        audit=audit(output, ordered, known_gaps=config.known_survey_gaps),
        skipped=skipped,
    )


def survey_range(rows: list[OutputRow]) -> tuple[int | None, int | None]:
    numbers = [n for n in (survey_number(r.survey) for r in rows) if n is not None]
    return (min(numbers), max(numbers)) if numbers else (None, None)
