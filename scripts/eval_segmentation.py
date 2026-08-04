"""Score the segmenters against the golden set's known geometry.

There are two things we can check without labelling a single pixel:

* **Page side** -- which half of the spread each photo shows is recorded in
  ``tests/unit/test_layout_bootstrap.py``;
* **Row count** -- the manifest says exactly which workbook rows each photo
  covers, so the number of handwritten lines on it is known.

Run:

    python scripts/eval_segmentation.py            # both segmenters
    python scripts/eval_segmentation.py --overlays # also write pictures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ocr.layout import bootstrap, ruled  # noqa: E402
from local_ocr.paths import PAGE_MANIFEST, SAMPLES_DIR  # noqa: E402

IMAGES = SAMPLES_DIR / "images_lowres"

#: Which half of the spread each sample photo shows, read off the images.
TARGET_SIDE = {
    "page_01": "left", "page_02": "left", "page_03": "left", "page_04": "right",
    "page_05": "left", "page_06": "right", "page_07": "left", "page_08": "right",
    "page_09": "left", "page_10": "right", "page_11": "left", "page_12": "right",
}


def expected_rows() -> dict[str, int]:
    manifest = yaml.safe_load(PAGE_MANIFEST.read_text(encoding="utf-8"))
    return {
        page["id"]: page["last_row"] - page["first_row"] + 1
        for page in manifest["pages"]
    }


def evaluate(name: str, analyse, expected: dict[str, int], overlays: Path | None) -> None:
    side_ok = 0
    row_error = 0
    exact = 0
    within_two = 0
    pages = 0

    print(f"\n=== {name} ===")
    print(f"{'page':<9} {'side':<7} {'rows':>5} {'want':>5} {'diff':>5}  rules")
    for page_id in sorted(expected):
        matches = sorted(IMAGES.glob(f"{page_id}.*"))
        if not matches:
            continue
        image = cv2.imread(str(matches[0]))
        geometry = analyse(image)
        if geometry is None:
            print(f"{page_id:<9} {'FAILED':<7}")
            pages += 1
            continue

        pages += 1
        want = expected[page_id]
        got = len(geometry.rows)
        diff = got - want
        correct_side = geometry.side == TARGET_SIDE[page_id]
        side_ok += correct_side
        row_error += abs(diff)
        exact += diff == 0
        within_two += abs(diff) <= 2

        flag = "" if correct_side else "  <- wrong side"
        print(
            f"{page_id:<9} {geometry.side:<7} {got:>5} {want:>5} {diff:>+5}  "
            f"{geometry.detected_rules:>5}{flag}"
        )

        if overlays is not None:
            overlays.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(
                str(overlays / f"{name}_{page_id}.png"),
                bootstrap.draw_overlay(image, geometry),
            )

    if pages:
        print(
            f"side {side_ok}/{pages} correct | rows exact {exact}/{pages} | "
            f"within 2: {within_two}/{pages} | mean |row error| "
            f"{row_error / pages:.1f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overlays", action="store_true")
    args = parser.parse_args()

    overlays = (SAMPLES_DIR / "crops" / "overlays") if args.overlays else None
    expected = expected_rows()
    evaluate("bootstrap", bootstrap.analyse, expected, overlays)
    evaluate("ruled", ruled.segment, expected, overlays)


if __name__ == "__main__":
    main()
