"""Batch assembly: page ordering, groups that straddle pages, remark binding, audits."""

from __future__ import annotations

import pytest

from local_ocr.assembly import assemble, audit, describe, order_pages
from local_ocr.correction import Corrector
from local_ocr.models import Cell, Column, OutputRow, PageResult, RowRecord


@pytest.fixture(scope="module")
def corrector() -> Corrector:
    return Corrector()


def page(page_id: str, *lines: tuple[str, str, str]) -> list[RowRecord]:
    return [
        RowRecord(
            page=page_id,
            index=index,
            survey=Cell(column=Column.SURVEY, raw_text=survey, confidence=1.0),
            meter=Cell(column=Column.METER, raw_text=meter, confidence=1.0),
            remarks=Cell(column=Column.REMARKS, raw_text=remarks, confidence=1.0),
        )
        for index, (survey, meter, remarks) in enumerate(lines)
    ]


def as_page(corrector: Corrector, page_id: str, *lines: tuple[str, str, str]) -> PageResult:
    rows = corrector.correct_page(page(page_id, *lines))
    return PageResult(path=page_id, rows=rows, detected_rules=len(rows))


class TestPageOrdering:
    def test_pages_are_ordered_by_survey_number_not_filename(self, corrector):
        """The sample photos are named by WhatsApp timestamp and run backwards.

        "1.42.58 PM" holds KE-680..683, the *last* pages of the notebook, so
        sorting by filename would reverse the whole batch.
        """
        later = as_page(corrector, "1.42.58 PM", ("680", "TY 51697", ""))
        earlier = as_page(corrector, "1.43.14 PM", ("570", "No meter", "Light in use"))

        assert [p.path for p in order_pages([later, earlier])] == ["1.43.14 PM", "1.42.58 PM"]

    def test_pages_without_a_survey_number_go_last(self, corrector):
        numbered = as_page(corrector, "numbered", ("600", "SEU 13229", ""))
        blank = as_page(corrector, "blank", ("", "SFL 88620", ""))
        assert [p.path for p in order_pages([blank, numbered])] == ["numbered", "blank"]


class TestGroupsAcrossPages:
    def test_a_rewritten_survey_number_does_not_start_a_second_group(self, corrector):
        """KE-619 spans page_04 and page_05; the workbook labels it once (row 690).

        The surveyor rewrites "619" at the top of the continuing page, which a
        per-page exporter would turn into a duplicate KE-619 group.
        """
        first = as_page(
            corrector,
            "page_04",
            ("619", "SFS 47649", ""),
            ("", "SFC 77080", ""),
            ("", "SCE 59523", ""),
        )
        second = as_page(
            corrector,
            "page_05",
            ("619", "SFE 83504", ""),
            ("", "SFG 49357", ""),
            ("", "SEP 34088", ""),
            ("620", "SEO 77469", ""),
        )

        batch = assemble([first, second])
        surveys = [row.survey for row in batch.rows]
        assert surveys == [
            "KE-619", None, None,
            None, None, None,
            "KE-620",
        ]
        assert [row.meter for row in batch.rows][:4] == [
            "SFS47649", "SFC77080", "SCE59523", "SFE83504",
        ]


class TestRemarkBinding:
    def test_a_remark_binds_to_a_group_that_started_on_the_previous_page(self, corrector):
        """page_11's "3 Shops" belongs to KE-673, whose first row is on page_10.

        Workbook row 836 carries it. Binding therefore cannot be done per page.
        """
        first = as_page(
            corrector,
            "page_10",
            ("673", "SFH 36183", ""),
            ("", "SFH 36184", ""),
            ("", "SFE 76273", ""),
            ("", "SCP 17740", ""),
        )
        second = as_page(
            corrector,
            "page_11",
            ("", "SCP 53831", "3 Shops"),
            ("", "SCP 53832", ""),
            ("674", "SES 57142", "Shop"),
        )

        batch = assemble([first, second])
        assert batch.rows[0].survey == "KE-673"
        assert batch.rows[0].remarks == "3 Shops"
        assert [row.remarks for row in batch.rows[1:5]] == [None, None, None, None]
        assert batch.rows[6].remarks == "Shop"

    def test_standalone_text_moves_up_to_the_group_row(self, corrector):
        """page_08: "Shop" is written beside the third meter of KE-646.

        The workbook records it on KE-646's first row (row 770).
        """
        result = as_page(
            corrector,
            "page_08",
            ("646", "SFY 36779", ""),
            ("", "SFG 79346", ""),
            ("", "SFF 96271", "Shop"),
        )
        batch = assemble([result])
        assert [row.remarks for row in batch.rows] == ["Shop", None, None]

    def test_text_sharing_a_line_with_a_side_code_stays_put(self, corrector):
        """page_09: "+37 Shop" beside SCO76794, a continuation row of KE-665.

        The workbook keeps it there as '-37,SHOP' (row 811) rather than moving
        the text to the group row. A side code makes the remark meter-specific.
        """
        result = as_page(
            corrector,
            "page_09",
            ("665", "SAT 25453", ""),
            ("", "SCA 84852", "+37"),
            ("", "SCO 76794", "+37 Shop"),
        )
        batch = assemble([result])
        assert [row.remarks for row in batch.rows] == [None, -37, "-37,Shop"]

    def test_two_remark_lines_in_one_group_are_joined_and_flagged(self, corrector):
        """page_05: "DC both side" / "DC water plant use" become one cell.

        Workbook row 696 reads 'DC Both Side, DC Water Plant Use'. The surveyor's
        own joiner varies between ", " and " + ", so a joined remark is always
        put in front of a human.
        """
        result = as_page(
            corrector,
            "page_05",
            ("620", "SEO 77469", "DC both side"),
            ("", "SFC 92490", "DC water plant use"),
            ("", "SCP 03033", ""),
        )
        batch = assemble([result])
        assert batch.rows[0].remarks == "DC both side, DC water plant use"
        assert batch.rows[0].status.needs_review


class TestAudits:
    def test_a_missing_page_shows_up_as_a_survey_gap(self):
        rows = [
            OutputRow(survey="KE-600", meter="SEU13229"),
            OutputRow(survey=None, meter="SEE14782"),
            OutputRow(survey="KE-604", meter="SEU13226"),
        ]
        result = audit(rows)
        assert result.survey_gaps == [(600, 604)]
        assert not result.clean
        assert "3 numbers missing" in describe(result)[0]

    def test_a_known_surveyor_gap_is_not_reported(self):
        """The reference workbook jumps 325 -> 336; that is the surveyor's gap."""
        rows = [
            OutputRow(survey="KE-325", meter="SCA48484"),
            OutputRow(survey="KE-336", meter="SFY32162"),
        ]
        assert audit(rows, known_gaps={(325, 336)}).clean

    def test_a_page_photographed_twice_shows_up_as_duplicates(self):
        rows = [
            OutputRow(survey="KE-680", meter="TY51697"),
            OutputRow(survey="KE-681", meter="SFC77214"),
            OutputRow(survey="KE-680", meter="TY51697"),
        ]
        result = audit(rows)
        assert result.duplicate_meters == ["TY51697"]
        assert result.survey_repeats == [680]

    def test_placeholders_are_allowed_to_repeat(self):
        rows = [
            OutputRow(survey="KE-351", meter="NoMeter"),
            OutputRow(survey="KE-599", meter="NoMeter"),
        ]
        assert audit(rows).duplicate_meters == []

    def test_a_dropped_row_shows_up_as_a_count_mismatch(self, corrector):
        result = as_page(corrector, "page_12", ("680", "TY 51697", ""), ("", "PSA 35432", ""))
        result.detected_rules = 3  # the layout stage saw three lines with ink
        batch = assemble([result])
        assert batch.audit.row_count_mismatches == [("page_12", 3, 2)]

    def test_a_clean_batch_says_so(self, corrector):
        result = as_page(corrector, "page_12", ("680", "TY 51697", ""), ("", "PSA 35432", ""))
        batch = assemble([result])
        assert batch.audit.clean
        assert "clean" in describe(batch.audit)[0]


class TestNothingIsDropped:
    def test_section_marker_rows_are_recorded_not_discarded(self, corrector):
        result = as_page(
            corrector,
            "page_12",
            ("683", "SET 84321", ""),
            ("", 'End Block "C"', ""),
        )
        batch = assemble([result])
        assert len(batch.rows) == 1
        assert len(batch.skipped) == 1
        assert "section marker" in batch.skipped[0][1]
