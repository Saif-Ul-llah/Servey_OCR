"""The correction stage must never damage a correct reading.

Every rule in local_ocr.correction exists to repair broken OCR, and each one is
a chance to break something that was already right. This test feeds the whole
golden set through the corrector as if the recognizer had been perfect -- meter
codes written the way they appear on the page, prefix and digits separated by a
space -- and asserts all 287 rows come back byte-identical to the reference
workbook.

It is the guard that lets the confusion tables be tuned aggressively: if a new
substitution starts eating good values, this fails immediately.
"""

from __future__ import annotations

import csv
import re

import pytest

from local_ocr.correction import Corrector
from local_ocr.models import Cell, Column, RowRecord
from local_ocr.paths import GROUND_TRUTH

#: How each non-meter placeholder is actually written by hand.
AS_WRITTEN = {
    "NoMeter": "No meter",
    "EmptyPlot": "Empty plot",
    "Empty": "Empty",
    "Plot": "Plot",
    "MeterMissing": "Meter missing",
}

METER_RE = re.compile(r"^([A-Z]{2,3})(\d+)$")


def as_written(meter: str) -> str:
    """Render a workbook meter value the way it appears on the notebook page."""
    if meter in AS_WRITTEN:
        return AS_WRITTEN[meter]
    match = METER_RE.match(meter)
    return f"{match.group(1)} {match.group(2)}" if match else meter


def load_golden() -> dict[str, list[dict[str, str]]]:
    pages: dict[str, list[dict[str, str]]] = {}
    with GROUND_TRUTH.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pages.setdefault(row["page_id"], []).append(row)
    return pages


pytestmark = pytest.mark.skipif(
    not GROUND_TRUTH.exists(),
    reason="run scripts/build_golden_set.py first",
)


@pytest.fixture(scope="module")
def golden() -> dict[str, list[dict[str, str]]]:
    return load_golden()


def test_golden_set_is_the_expected_shape(golden):
    total = sum(len(rows) for rows in golden.values())
    assert len(golden) == 12
    assert total == 287, "the 12 sample photos cover workbook rows 596-882"


@pytest.mark.parametrize("page_id", [f"page_{n:02d}" for n in range(1, 13)])
def test_perfect_input_survives_correction_unchanged(corrector_module, golden, page_id):
    expected = golden[page_id]

    records = [
        RowRecord(
            page=page_id,
            index=int(row["row_index"]),
            survey=Cell(column=Column.SURVEY, raw_text=row["survey"].replace("KE-", "")),
            meter=Cell(column=Column.METER, raw_text=as_written(row["meter"])),
            remarks=Cell(column=Column.REMARKS, raw_text=""),
        )
        for row in expected
    ]

    corrector_module.correct_page(records)

    assert [r.meter.text for r in records] == [row["meter"] for row in expected]
    assert [r.survey.text for r in records] == [row["survey"] for row in expected]


@pytest.fixture(scope="module")
def corrector_module() -> Corrector:
    return Corrector()
