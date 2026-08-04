"""Glue between the UI's rows and the deterministic `local_ocr` core.

The core was built to take page results, correct them, assemble them across
pages, and export a workbook. This module drives exactly that path from a flat
list of rows -- whether those rows came from Gemini or were typed by hand -- and
returns a review grid rich enough for the UI (per-cell status, alternative
readings, which rules fired) alongside the batch-level audit.

Two entry points:

* :func:`process` -- run correction + cross-page assembly over raw rows and
  return the reviewable grid + audit. Used by both "Recognise with Gemini" and
  the manual "Validate" button. Correction treats each field as *raw*, so typed
  or recognised text is normalised the same way.
* :func:`export` -- write the (human-finalised) grid to the reference workbook
  verbatim, so a reviewer's edits are never second-guessed.

Nothing is dropped silently: rows the pipeline sets aside (section markers,
blank lines) are returned in ``skipped`` with a reason, mirroring the core's
own contract.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from local_ocr.assembly.audit import audit
from local_ocr.assembly.batch import (
    AssemblyConfig,
    _flatten,
    assign_groups,
    bind_remarks,
    survey_range,
)
from local_ocr.correction.pipeline import Corrector
from local_ocr.correction.text_rules import format_remarks
from local_ocr.export.xlsx_exporter import write_workbook
from local_ocr.models import Cell, Column, OutputRow, PageResult, RowRecord, Status

_NUMERIC_RE = re.compile(r"^-?\d+$")
#: The survey column holds a bare plot number; the "KE-" is a constant the
#: exporter re-adds. A recognizer that writes "KE-683" back into the cell must
#: not have its letters fed to digit-coercion (which would read "KE" as "3").
_SURVEY_PREFIX_RE = re.compile(r"^\s*KE[-\s]*", re.IGNORECASE)


def _make_row(page: str, index: int, survey: str, meter: str, remarks: str) -> RowRecord:
    """Build a fresh RowRecord whose cells carry the input as raw text."""
    survey = _SURVEY_PREFIX_RE.sub("", survey)
    return RowRecord(
        page=page,
        index=index,
        survey=Cell(column=Column.SURVEY, raw_text=survey, text=survey),
        meter=Cell(column=Column.METER, raw_text=meter, text=meter),
        remarks=Cell(column=Column.REMARKS, raw_text=remarks, text=remarks),
    )


def _cell_view(cell: Cell) -> dict:
    return {
        "text": cell.text,
        "raw": cell.raw_text,
        "status": cell.status.value,
        "needs_review": cell.status.needs_review,
        "candidates": list(cell.candidates),
        "rules": list(cell.applied_rules),
    }


def process(pages: list[tuple[str, list[dict]]], config: AssemblyConfig | None = None) -> dict:
    """Correct and assemble raw rows; return the review grid + audit.

    ``pages`` is ``[(page_name, [{survey, meter, remarks}, ...]), ...]`` in the
    order the rows were read on each page. Page ordering across the batch is
    decided by survey range, exactly as in a normal run.
    """
    config = config or AssemblyConfig()
    corrector = Corrector()

    page_results: list[PageResult] = []
    for page_name, raw_rows in pages:
        records = [
            _make_row(
                page_name,
                index,
                str(raw.get("survey", "") or ""),
                str(raw.get("meter", "") or ""),
                str(raw.get("remarks", "") or ""),
            )
            for index, raw in enumerate(raw_rows)
        ]
        corrector.correct_page(records)
        page_results.append(PageResult(path=page_name, rows=records))

    # Replicate assemble()'s steps so the rich per-line records and the
    # batch-level audit come from a single grouping pass (assign_groups mutates
    # the records, so it must run exactly once).
    ordered = _order_pages(page_results)
    kept, dropped = _flatten(ordered)
    group_ids = assign_groups(kept)
    group_remarks = bind_remarks(kept, group_ids, config)

    grid: list[dict] = []
    output_rows: list[OutputRow] = []
    for record, group_id in zip(kept, group_ids):
        if record.annotations:
            bound = record.remarks.text.strip()
        elif record.is_group_first:
            bound = group_remarks.get(group_id, "")
        else:
            bound = ""
        remarks_value = format_remarks(bound, record.annotations)

        survey_display = record.group_survey if record.is_group_first else ""
        output_rows.append(
            OutputRow(
                survey=record.group_survey if record.is_group_first else None,
                meter=record.meter.text,
                remarks=remarks_value,
                page=record.page,
                row_index=record.index,
                status=record.status,
            )
        )
        grid.append(
            {
                "id": f"{record.page}#{record.index}",
                "page": record.page,
                "index": record.index,
                "group_id": group_id,
                "is_group_first": record.is_group_first,
                "survey": survey_display or "",
                "meter": record.meter.text,
                "remarks": "" if remarks_value is None else str(remarks_value),
                "annotations": list(record.annotations),
                "cells": {
                    "survey": _cell_view(record.survey),
                    "meter": _cell_view(record.meter),
                    "remarks": _cell_view(record.remarks),
                },
                "needs_review": record.needs_review,
                "confidence": round(record.min_confidence, 3),
            }
        )

    report = audit(output_rows, ordered, known_gaps=config.known_survey_gaps)
    lo, hi = survey_range(output_rows)

    return {
        "rows": grid,
        "audit": {
            "clean": report.clean,
            "survey_gaps": [list(g) for g in report.survey_gaps],
            "survey_repeats": report.survey_repeats,
            "duplicate_meters": report.duplicate_meters,
            "row_count_mismatches": [list(m) for m in report.row_count_mismatches],
        },
        "skipped": [
            {"page": rec.page, "index": rec.index, "reason": reason} for rec, reason in dropped
        ],
        "summary": {
            "row_count": len(grid),
            "needs_review": sum(1 for r in grid if r["needs_review"]),
            "survey_range": [lo, hi],
            "skipped": len(dropped),
        },
    }


def _order_pages(pages: list[PageResult]) -> list[PageResult]:
    """Order by first survey number; unnumbered pages keep input order at the end."""
    numbered: list[tuple[int, int, PageResult]] = []
    unnumbered: list[tuple[int, PageResult]] = []
    for position, page in enumerate(pages):
        numbers = page.survey_numbers
        if numbers:
            numbered.append((numbers[0], position, page))
        else:
            unnumbered.append((position, page))
    numbered.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in numbered] + [p for _, p in unnumbered]


def _coerce_remarks(value: str | None):
    """Reproduce the reference workbook's cell types from a grid string.

    A lone side code such as ``-37`` is a numeric cell; anything else is text;
    blank is an empty cell.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _NUMERIC_RE.match(text):
        return int(text)
    return text


def export(rows: list[dict], out_path: Path | str, backup_previous: bool = True) -> Path:
    """Write the review grid to the reference workbook, verbatim.

    The grid is already the human-approved answer, so this does not re-run
    correction or grouping -- it writes what the reviewer sees, only coercing
    remarks back to the int/text split the reference file uses.
    """
    output_rows = [
        OutputRow(
            survey=(str(r.get("survey", "")).strip() or None),
            meter=str(r.get("meter", "")).strip(),
            remarks=_coerce_remarks(r.get("remarks")),
        )
        for r in rows
    ]
    return write_workbook(output_rows, out_path, backup_previous=backup_previous)
