"""The deliverable must be a structural clone of the reference workbook.

Every assertion here is checked against
"Samples or examle data/SACHAL SURVEY - IMAGES DATA.xlsx" so that the existing
downstream process cannot tell an exported file from a hand-typed one.
"""

from __future__ import annotations

import openpyxl
import pytest

from local_ocr.export import (
    COLUMN_WIDTHS,
    HEADERS,
    SHEET_NAME,
    read_workbook,
    sidecar_path,
    write_workbook,
)
from local_ocr.models import OutputRow
from local_ocr.paths import REPO_ROOT

REFERENCE = REPO_ROOT / "Samples or examle data" / "SACHAL SURVEY - IMAGES DATA.xlsx"

SAMPLE_ROWS = [
    OutputRow(survey="KE-680", meter="TY51697"),
    OutputRow(survey=None, meter="PSA35432"),
    OutputRow(survey="KE-681", meter="SFC77214", remarks="Blush by Sobi's Parlour"),
    OutputRow(survey=None, meter="SCA84852", remarks=-37),
    OutputRow(survey=None, meter="SCO76794", remarks="-37,SHOP"),
    OutputRow(survey="KE-599", meter="NoMeter", remarks="Vacant Plot"),
]


@pytest.fixture
def written(tmp_path):
    path = write_workbook(SAMPLE_ROWS, tmp_path / "out.xlsx", backup_previous=False)
    wb = openpyxl.load_workbook(path)
    yield path, wb
    wb.close()


class TestMatchesReference:
    @pytest.mark.skipif(not REFERENCE.exists(), reason="reference workbook not present")
    def test_sheet_name_and_headers_come_from_the_reference(self):
        wb = openpyxl.load_workbook(REFERENCE, read_only=True)
        ws = wb.active
        assert ws.title == SHEET_NAME
        assert tuple(ws.cell(row=1, column=c).value for c in (1, 2, 3)) == HEADERS
        wb.close()

    def test_the_header_misspelling_is_preserved(self):
        # "SERVEY #" is a typo in the source file, but the existing workflow
        # references it, so correcting it would break the deliverable.
        assert HEADERS[0] == "SERVEY #"

    @pytest.mark.skipif(not REFERENCE.exists(), reason="reference workbook not present")
    def test_column_widths_match_the_reference(self):
        wb = openpyxl.load_workbook(REFERENCE)
        ws = wb.active
        assert tuple(ws.column_dimensions[c].width for c in "ABC") == COLUMN_WIDTHS
        wb.close()

    def test_written_file_has_one_sheet_only(self, written):
        _, wb = written
        assert wb.sheetnames == [SHEET_NAME]


class TestCellTypes:
    def test_a_continuation_survey_cell_is_empty_not_an_empty_string(self, written):
        _, wb = written
        ws = wb.active
        assert ws.cell(row=3, column=1).value is None
        assert ws.cell(row=2, column=1).value == "KE-680"

    def test_a_lone_side_code_is_written_as_a_number(self, written):
        _, wb = written
        ws = wb.active
        cell = ws.cell(row=5, column=3)
        assert cell.value == -37
        assert cell.data_type == "n"

    def test_a_combined_side_code_is_written_as_text(self, written):
        _, wb = written
        ws = wb.active
        cell = ws.cell(row=6, column=3)
        assert cell.value == "-37,SHOP"
        assert cell.data_type == "s"

    def test_an_absent_remark_leaves_the_cell_empty(self, written):
        _, wb = written
        ws = wb.active
        assert ws.cell(row=2, column=3).value is None

    def test_apostrophes_survive(self, written):
        _, wb = written
        assert wb.active.cell(row=4, column=3).value == "Blush by Sobi's Parlour"


class TestRoundTrip:
    def test_reading_back_preserves_values_and_types(self, tmp_path):
        path = write_workbook(SAMPLE_ROWS, tmp_path / "rt.xlsx", backup_previous=False)
        reloaded = read_workbook(path)
        assert len(reloaded) == len(SAMPLE_ROWS)
        assert [r.survey for r in reloaded] == [r.survey for r in SAMPLE_ROWS]
        assert [r.meter for r in reloaded] == [r.meter for r in SAMPLE_ROWS]
        assert [r.remarks for r in reloaded] == [r.remarks for r in SAMPLE_ROWS]
        assert isinstance(reloaded[3].remarks, int)


class TestBackup:
    def test_an_existing_file_is_copied_aside_before_overwriting(self, tmp_path):
        path = tmp_path / "out.xlsx"
        write_workbook(SAMPLE_ROWS[:2], path, backup_previous=False)
        write_workbook(SAMPLE_ROWS, path, backup_previous=True)

        backups = list(tmp_path.glob("out.backup_*.xlsx"))
        assert len(backups) == 1
        assert len(read_workbook(backups[0])) == 2
        assert len(read_workbook(path)) == len(SAMPLE_ROWS)


def test_sidecar_is_a_separate_file(tmp_path):
    deliverable = tmp_path / "SERVEY_OCR_20260801.xlsx"
    assert sidecar_path(deliverable).name == "SERVEY_OCR_20260801_review.xlsx"
    assert sidecar_path(deliverable) != deliverable
