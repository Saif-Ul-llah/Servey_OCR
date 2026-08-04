"""Table-driven tests for the domain correction rules.

Every case here is a *real* one lifted from the sample photographs, with the
expected value taken from the corresponding row of
"Samples or examle data/SACHAL SURVEY - IMAGES DATA.xlsx". The page and
workbook row are named in each case so a failure can be checked against the
source in seconds.
"""

from __future__ import annotations

import pytest

from local_ocr.correction import (
    Corrector,
    default_whitelist,
    expand_meter_ditto,
    extract_annotations,
    format_remarks,
    is_section_marker,
    is_valid_meter,
    load_section_markers,
    parse_meter,
    repair_meter,
    resolve_corrected_digits,
    strip_non_latin,
)
from local_ocr.correction.tokens import SpecialTokens
from local_ocr.models import Cell, Column, RowRecord, Status


@pytest.fixture(scope="module")
def corrector() -> Corrector:
    return Corrector()


def make_row(page: str, index: int, survey: str = "", meter: str = "", remarks: str = "") -> RowRecord:
    return RowRecord(
        page=page,
        index=index,
        survey=Cell(column=Column.SURVEY, raw_text=survey),
        meter=Cell(column=Column.METER, raw_text=meter),
        remarks=Cell(column=Column.REMARKS, raw_text=remarks),
    )


# --------------------------------------------------------------------- grammar


class TestGrammar:
    def test_whitelist_has_every_prefix_in_the_reference_workbook(self):
        # scripts/derive_prefixes.py found exactly 65 across all 881 rows.
        assert len(default_whitelist()) == 65

    @pytest.mark.parametrize(
        "text,prefix,digits",
        [
            ("SFT77430", "SFT", "77430"),   # page_12, workbook row 873
            ("SFT 77430", "SFT", "77430"),  # the space is how it is written
            ("TY51697", "TY", "51697"),     # page_12, row 864
            ("PSA35432", "PSA", "35432"),   # page_12, row 865
            ("LA494585", "LA", "494585"),   # the only 6-digit family
        ],
    )
    def test_parses_attested_codes(self, text, prefix, digits):
        meter = parse_meter(text)
        assert meter is not None
        assert (meter.prefix, meter.digits) == (prefix, digits)

    @pytest.mark.parametrize("text", ["SFT7743", "S77430", "SFTX7430", "77430", ""])
    def test_rejects_malformed_codes(self, text):
        assert parse_meter(text) is None

    def test_unknown_prefix_is_not_valid(self):
        assert not is_valid_meter("SQZ12345")
        assert is_valid_meter("SFT77430")


# ------------------------------------------------------------ character repair


class TestCharConfusion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("5FT77430", "SFT77430"),   # 5 in a letter slot can only be S
            ("SFP 93O95", "SFP93095"),  # O in a digit slot can only be 0
            ("P5A 33901", "PSA33901"),  # page_12, row 869
            ("SCE 1O194", "SCE10194"),  # page_07, row 750
            ("5EG 74422", "SEG74422"),  # page_09, row 794
        ],
    )
    def test_type_errors_are_repaired(self, raw, expected):
        candidates = repair_meter(raw)
        assert candidates, f"no candidate produced for {raw!r}"
        assert candidates[0].text == expected

    def test_digits_are_never_substituted_for_other_digits(self):
        # 83628 must not silently become 83629 or 88628. With no master meter
        # list there is nothing to confirm such a change, so it is not made.
        candidates = repair_meter("SFL83628")
        assert all(c.digits == "83628" for c in candidates)

    def test_two_letter_prefix_is_not_rewritten(self):
        # "SF" is a 3-letter prefix with a character missing, not a typo for the
        # one-off whitelist entry "SE". Refusing to guess is the correct outcome.
        assert repair_meter("SF 77430") == []

    def test_ambiguous_three_letter_prefix_offers_alternatives(self):
        # SCI and SCT are both real prefixes and look alike in this hand
        # (page_12 row 879 is SCT79031). The recognizer cannot settle it, so the
        # alternative must reach the review UI.
        candidates = repair_meter("SCI 79031")
        assert candidates[0].text == "SCI79031"
        assert "SCT79031" in [c.text for c in candidates]


# ------------------------------------------------------------------ repeat marks


class TestDitto:
    @pytest.mark.parametrize(
        "raw,inherited,expected",
        [
            ('" 70028', "SCF", "SCF70028"),   # page_09, workbook row 791
            ('" 96344', "SCS", "SCS96344"),   # page_09, row 793
            ('" 95090', "SFF", "SFF95090"),   # page_09, row 814
            ('" 63727', "SEG", "SEG63727"),   # page_08, row 785
            ('" 76286', "SFE", "SFE76286"),   # page_08, row 787
            ('" 38123', "SFY", "SFY38123"),   # page_08, row 779
        ],
    )
    def test_meter_ditto_inherits_prefix(self, raw, inherited, expected):
        assert expand_meter_ditto(raw, inherited).text == expected

    def test_meter_ditto_crosses_a_survey_group_boundary(self, corrector):
        """page_09: KE-662's line is a repeat mark, inheriting from KE-661.

        Workbook rows 804-805 are SCT89642 (KE-661) then SCT45386 (KE-662). If
        the inherited prefix reset at group boundaries this row would be lost.
        """
        rows = [
            make_row("page_09", 0, survey="661", meter="SCT 89642"),
            make_row("page_09", 1, survey="662", meter='" 45386'),
        ]
        corrector.correct_page(rows)
        assert rows[0].meter.text == "SCT89642"
        assert rows[1].meter.text == "SCT45386"
        assert "ditto_meter" in rows[1].meter.applied_rules

    def test_meter_ditto_without_a_prefix_is_flagged_not_guessed(self, corrector):
        rows = [make_row("page_09", 0, survey="655", meter='" 70028')]
        corrector.correct_page(rows)
        assert rows[0].meter.status is Status.MANUAL_REVIEW
        assert "ditto_no_prefix" in rows[0].meter.applied_rules

    def test_remarks_ditto_repeats_the_text_above(self, corrector):
        """page_03: "Imam Bargah" is written once with a mark beneath it.

        Workbook rows 644 and 646 both carry the text.
        """
        rows = [
            make_row("page_03", 0, survey="602", meter="SES 07953", remarks="Imam Bargah"),
            make_row("page_03", 1, meter="SFL 83612"),
            make_row("page_03", 2, survey="603", meter="PSA 40024", remarks='"'),
        ]
        corrector.correct_page(rows)
        assert rows[0].remarks.text == "Imam Bargah"
        assert rows[2].remarks.text == "Imam Bargah"
        assert "ditto_remark" in rows[2].remarks.applied_rules


# -------------------------------------------------------------- correction marks


class TestCorrectionGlyphs:
    def test_strike_and_rewrite_keeps_the_last_run(self, corrector):
        """page_09: "SFL 836 83628" -- 836 is struck out. Workbook row 802."""
        rows = [make_row("page_09", 0, meter="SFL 836 83628")]
        corrector.correct_page(rows)
        assert rows[0].meter.text == "SFL83628"
        assert "struck_out_correction" in rows[0].meter.applied_rules

    def test_arrow_confirms_the_pointed_to_value(self, corrector):
        """page_07: "SCE 10194 <- 10194". Workbook row 750."""
        rows = [make_row("page_07", 0, meter="SCE 10194 <- 10194")]
        corrector.correct_page(rows)
        assert rows[0].meter.text == "SCE10194"

    def test_rewrite_outside_the_bracket_keeps_the_last_run(self, corrector):
        """page_01: "SCO 81376 | 81376". Workbook row 619."""
        rows = [make_row("page_01", 0, meter="SCO 81376 81376")]
        corrector.correct_page(rows)
        assert rows[0].meter.text == "SCO81376"

    def test_two_different_complete_runs_are_flagged_not_guessed(self, corrector):
        rows = [make_row("page_09", 0, meter="SCC 68610 68674")]
        corrector.correct_page(rows)
        assert rows[0].meter.status is Status.AMBIGUOUS
        assert rows[0].meter.candidates == ["68610", "68674"]

    def test_struck_out_survey_number_keeps_the_last_run(self, corrector):
        """page_10 heads a group "678 668" with 678 struck out. Workbook row 817."""
        rows = [make_row("page_10", 0, survey="678 668", meter="SCO 19291")]
        corrector.correct_page(rows)
        assert rows[0].survey.text == "KE-668"
        assert "survey_struck_out" in rows[0].survey.applied_rules


# ------------------------------------------------------------ placeholder values


class TestSpecialTokens:
    @pytest.mark.parametrize(
        "raw",
        ["No meter", "no meter", "No metd", "NO METER", "no  meter"],
    )
    def test_no_meter_variants_normalise(self, corrector, raw):
        """page_02 row 640 and page_07 row 753 both read "No met..." on the page."""
        rows = [make_row("page_02", 0, survey="599", meter=raw, remarks="Vacant plot")]
        corrector.correct_page(rows)
        assert rows[0].meter.text == "NoMeter"

    def test_every_placeholder_in_the_workbook_is_covered(self):
        tokens = SpecialTokens.load()
        # These five are the complete set of non-meter values in the reference.
        assert set(tokens.outputs) == {
            "NoMeter",
            "MeterMissing",
            "EmptyPlot",
            "Empty",
            "Plot",
        }

    def test_a_real_remark_is_not_mistaken_for_a_placeholder(self):
        tokens = SpecialTokens.load()
        assert tokens.match("Demolished building, under construction") is None
        assert tokens.match("Imam Bargah") is None


# --------------------------------------------------------------------- remarks


class TestRemarks:
    def test_urdu_is_dropped_but_english_on_the_same_page_is_kept(self):
        """page_10 carries two Urdu remarks the workbook drops entirely."""
        cleaned, found = strip_non_latin("نکاس بری فروخت")
        assert found and cleaned == ""

        cleaned, found = strip_non_latin("کہ ریسٹورنٹ 4 Shops")
        assert found and cleaned == "4 Shops"

    @pytest.mark.parametrize(
        "raw,annotations,residual",
        [
            ("+37", ["-37"], ""),            # page_09, workbook row 810
            ("#37 +24", ["-37", "-24"], ""),  # page_09, row 794 -> '-37,-24'
            ("+42 +128", ["-42", "-128"], ""),  # page_01, row 612
            ("+37 Shop", ["-37"], "Shop"),   # page_09, row 811
            ("3 Shops", [], "3 Shops"),      # a count, not a side code
            ("99 School", [], "99 School"),  # workbook row 149
        ],
    )
    def test_side_codes_are_separated_from_text(self, raw, annotations, residual):
        found, rest = extract_annotations(raw)
        assert found == annotations
        assert rest == residual

    @pytest.mark.parametrize(
        "text,annotations,expected",
        [
            ("", ["-37"], -37),                    # lone code -> numeric cell
            ("", ["-128"], -128),                  # page_02, row 639
            ("", ["-37", "-24"], "-37,-24"),       # page_09, row 794
            ("", ["-42", "-128"], "-42,-128"),     # page_01, row 612
            ("SHOP", ["-37"], "-37,SHOP"),         # page_09, row 811
            ("3 Shops", [], "3 Shops"),            # page_11, row 854
            ("", [], None),                        # empty stays empty
        ],
    )
    def test_remarks_cell_matches_the_workbook_type_and_text(self, text, annotations, expected):
        assert format_remarks(text, annotations) == expected

    def test_a_lone_side_code_is_a_number_not_a_string(self):
        # The reference workbook stores -37 as a number; the export must too.
        assert isinstance(format_remarks("", ["-37"]), int)
        assert isinstance(format_remarks("", ["-37", "-24"]), str)


# -------------------------------------------------------------- section markers


class TestSectionMarkers:
    @pytest.mark.parametrize(
        "text",
        [
            'End Block "C"',      # page_12
            "End Block 'C'",
            "End Block C",
            "x - x - x",          # page_12 divider
            "X - X - X",
            "----------",
        ],
    )
    def test_structural_lines_are_recognised(self, text):
        assert is_section_marker(text, load_section_markers())

    @pytest.mark.parametrize("text", ["3 Shops", "Imam Bargah", "SFT 77430", "Unlock / DC"])
    def test_real_data_is_not_treated_as_structure(self, text):
        assert not is_section_marker(text, load_section_markers())

    def test_a_marker_row_is_skipped_not_exported(self, corrector):
        rows = [make_row("page_12", 0, meter='End Block "C"')]
        corrector.correct_page(rows)
        assert rows[0].meter.status is Status.SKIPPED
        assert rows[0].status is Status.SKIPPED


# ------------------------------------------------------------------ whole page


class TestPageCorrection:
    def test_page_09_hard_cases_resolve_together(self, corrector):
        """The densest page: repeat marks, a strike-out and side codes at once.

        Expected values are workbook rows 789-793 and 801-802, 810-811.
        """
        rows = [
            make_row("page_09", 0, survey="654", meter="SFL 88621"),
            make_row("page_09", 1, survey="655", meter="SCF 70029"),
            make_row("page_09", 2, meter='" 70028'),
            make_row("page_09", 3, meter="SCS 96343"),
            make_row("page_09", 4, meter='" 96344'),
            make_row("page_09", 5, survey="659", meter="SEL 54857"),
            make_row("page_09", 6, meter="SFL 83629"),
            make_row("page_09", 7, meter="SFL 836 83628"),
            make_row("page_09", 8, meter="SCA 84852", remarks="+37"),
            make_row("page_09", 9, meter="SCO 76794", remarks="+37 Shop"),
        ]
        corrector.correct_page(rows)

        assert [r.meter.text for r in rows] == [
            "SFL88621",
            "SCF70029",
            "SCF70028",
            "SCS96343",
            "SCS96344",
            "SEL54857",
            "SFL83629",
            "SFL83628",
            "SCA84852",
            "SCO76794",
        ]
        assert rows[0].survey.text == "KE-654"
        assert rows[8].annotations == ["-37"]
        assert rows[9].annotations == ["-37"]
        assert rows[9].remarks.text == "Shop"

    def test_group_first_flag_tracks_the_survey_column(self, corrector):
        rows = [
            make_row("page_08", 0, survey="645", meter="SFT 68201"),
            make_row("page_08", 1, meter="SFU 69314"),
            make_row("page_08", 2, meter="SEL 45660"),
            make_row("page_08", 3, survey="646", meter="SFY 36779"),
        ]
        corrector.correct_page(rows)
        assert [r.is_group_first for r in rows] == [True, False, False, True]
        assert [r.group_survey for r in rows if r.is_group_first] == ["KE-645", "KE-646"]
