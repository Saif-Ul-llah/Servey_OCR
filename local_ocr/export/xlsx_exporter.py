"""Write the deliverable workbook.

The output is a structural clone of
"Samples or examle data/SACHAL SURVEY - IMAGES DATA.xlsx": one sheet named
``New XLSX Worksheet``, three columns headed ``SERVEY #`` / ``METER#`` /
``REMARKS`` (the misspelling is preserved -- the existing workflow reads it),
no styling beyond the three column widths, and General number format
throughout.

Two details that a naive export gets wrong and that the tests pin down:

* a continuation row's survey cell is **genuinely empty**, not an empty string.
  ``openpyxl`` reports the two differently and downstream tools treat them
  differently;
* a row carrying only side codes stores a **number** (``-37``), while anything
  else is text (``'-42,-128'``, ``'-37,SHOP'``).

Review data never touches this file. It goes to a sidecar, so the deliverable
stays byte-compatible with what the current process consumes.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from local_ocr.models import OutputRow

SHEET_NAME = "New XLSX Worksheet"
HEADERS = ("SERVEY #", "METER#", "REMARKS")
#: Copied from the reference workbook so a diff of the two files is empty.
COLUMN_WIDTHS = (12.0, 17.5555555555556, 45.0)


def backup(path: Path) -> Path | None:
    """Move an existing output aside before overwriting it."""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.stem}.backup_{stamp}{path.suffix}")
    shutil.copy2(path, target)
    return target


def write_workbook(rows: list[OutputRow], path: Path | str, backup_previous: bool = True) -> Path:
    """Write `rows` to `path` in the reference format. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup_previous:
        backup(path)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET_NAME

    for column, header in enumerate(HEADERS, start=1):
        ws.cell(row=1, column=column, value=header)

    for offset, row in enumerate(rows, start=2):
        # Leave the cell untouched when there is no survey number: writing None
        # via `value=` still creates a cell, and writing "" creates a string.
        if row.survey:
            ws.cell(row=offset, column=1, value=row.survey)
        ws.cell(row=offset, column=2, value=row.meter)
        if row.remarks is not None and row.remarks != "":
            ws.cell(row=offset, column=3, value=row.remarks)

    for index, width in enumerate(COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    wb.save(path)
    return path


def read_workbook(path: Path | str) -> list[OutputRow]:
    """Read a workbook back into `OutputRow`s, preserving cell types.

    Used by the golden test to compare against the reference file, and by the
    review UI to reload a previous run.
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active

    rows: list[OutputRow] = []
    for survey, meter, remarks in ws.iter_rows(min_row=2, max_col=3, values_only=True):
        if survey is None and meter is None and remarks is None:
            continue
        rows.append(
            OutputRow(
                survey=None if survey is None else str(survey),
                meter="" if meter is None else str(meter),
                remarks=remarks,
            )
        )
    wb.close()
    return rows
