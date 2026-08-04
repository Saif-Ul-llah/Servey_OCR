"""Smoke tests for the bootstrap segmenter.

These pin down what it currently does, not what the finished layout stage must
do. The measured numbers are recorded in the assertions so that a change in
segmentation quality shows up as a test result rather than as a surprise in the
bake-off. When Phase 1 replaces this, these thresholds move up.
"""

from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")

from local_ocr.layout import analyse, draw_overlay, ink_mask, paper_region  # noqa: E402
from local_ocr.paths import SAMPLES_DIR  # noqa: E402

IMAGES = SAMPLES_DIR / "images_lowres"
PAGE_IDS = [f"page_{n:02d}" for n in range(1, 13)]

#: Which half of the spread each sample photo actually shows, read off the images.
TARGET_SIDE = {
    "page_01": "left", "page_02": "left", "page_03": "left", "page_04": "right",
    "page_05": "left", "page_06": "right", "page_07": "left", "page_08": "right",
    "page_09": "left", "page_10": "right", "page_11": "left", "page_12": "right",
}

pytestmark = pytest.mark.skipif(
    not IMAGES.exists() or not any(IMAGES.glob("page_*.jpeg")),
    reason="run scripts/build_golden_set.py --copy-images first",
)


def load(page_id: str):
    matches = sorted(IMAGES.glob(f"{page_id}.*"))
    assert matches, f"no image for {page_id}"
    return cv2.imread(str(matches[0]))


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_paper_region_is_found_and_plausible(page_id):
    image = load(page_id)
    region = paper_region(image)
    assert region is not None

    _, _, width, height = region
    coverage = (width * height) / (image.shape[0] * image.shape[1])
    # The open notebook fills roughly a third to two thirds of the frame.
    assert 0.25 < coverage < 0.80, f"{page_id}: paper covers {coverage:.0%} of the frame"


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_analyse_returns_three_columns_and_some_rows(page_id):
    geometry = analyse(load(page_id))
    assert geometry is not None
    assert len(geometry.columns) == 3
    assert geometry.rows, f"{page_id}: no lines of handwriting found"
    assert geometry.side in ("left", "right")


def test_ink_mask_separates_writing_from_the_printed_rules():
    ink, rules = ink_mask(load("page_12"))
    assert ink.shape == rules.shape
    assert (rules > 0).any(), "the notebook's printed rules should be detected"
    assert (ink > 0).any()


def test_overlay_renders_without_error():
    image = load("page_12")
    overlay = draw_overlay(image, analyse(image))
    assert overlay.shape == image.shape


def test_current_page_side_accuracy_is_recorded():
    """The bootstrap picks the right page of the spread on 9 of 12 samples.

    Recorded so a regression is visible. Phase 1 must raise this to 12/12;
    when it does, tighten this number rather than deleting the test.
    """
    correct = sum(1 for page_id in PAGE_IDS if analyse(load(page_id)).side == TARGET_SIDE[page_id])
    assert correct >= 9, f"page-side selection regressed to {correct}/12"
