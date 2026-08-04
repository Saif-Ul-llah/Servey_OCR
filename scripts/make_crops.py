"""Cut cell crops out of the sample photographs and label them from the golden set.

Feeds scripts/eval_engines.py. Crops land in::

    samples/crops/<page_id>/<row_index>_<column>.png
    samples/crops/labels.csv

The segmenter this uses is a supervised bootstrap, not the finished layout stage
-- it currently picks the right page of the spread on 9 of 12 sample photos and
gets row counts wrong on most. **Always pass --overlays and look at the
pictures.** Where the geometry is wrong, put the correct numbers in a geometry
file and pass --geometry; the format is documented below and one entry overrides
one page completely.

A page whose detected row count does not match the golden set is skipped by
default, because mislabelled crops would silently corrupt the bake-off. Pass
--allow-row-mismatch to override that, and expect the numbers to be worthless.

Geometry override file (YAML)::

    page_09:
      target: [90, 150, 700, 900]   # x, y, w, h of the page to transcribe
      rows: [[12, 46], [48, 82]]    # (top, bottom) per line, target-relative
      columns: [[0, 90], [95, 300], [310, 700]]

Usage:
    python scripts/make_crops.py --overlays
    python scripts/make_crops.py --images samples/images_hires --overlays
    python scripts/make_crops.py --geometry samples/geometry.yaml
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ocr.layout.bootstrap import PageGeometry, analyse, crop, draw_overlay  # noqa: E402
from local_ocr.paths import GROUND_TRUTH, REPO_ROOT, SAMPLES_DIR  # noqa: E402

COLUMNS = ("survey", "meter", "remarks")
LABEL_FIELDS = ("page_id", "row_index", "column", "crop", "expected")


def load_golden() -> dict[str, list[dict[str, str]]]:
    pages: dict[str, list[dict[str, str]]] = defaultdict(list)
    with GROUND_TRUTH.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pages[row["page_id"]].append(row)
    return pages


def load_geometry_overrides(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def apply_override(geometry: PageGeometry | None, override: dict) -> PageGeometry:
    base = geometry or PageGeometry(paper=(0, 0, 0, 0), target=(0, 0, 0, 0), side="?", gutter_x=0)
    if "target" in override:
        base.target = tuple(override["target"])  # type: ignore[assignment]
    if "rows" in override:
        base.rows = [tuple(r) for r in override["rows"]]
    if "columns" in override:
        base.columns = [tuple(c) for c in override["columns"]]
    return base


def expected_value(row: dict[str, str], column: str) -> str:
    if column == "survey":
        return row["survey"].replace("KE-", "")
    if column == "meter":
        return row["meter"]
    return row["remarks"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", type=Path, default=SAMPLES_DIR / "images_lowres")
    parser.add_argument("--out", type=Path, default=SAMPLES_DIR / "crops")
    parser.add_argument("--geometry", type=Path, default=None)
    parser.add_argument("--overlays", action="store_true", help="write a segmentation picture per page")
    parser.add_argument("--allow-row-mismatch", action="store_true")
    parser.add_argument("--pad", type=int, default=4)
    args = parser.parse_args(argv)

    if not GROUND_TRUTH.exists():
        print("error: run scripts/build_golden_set.py first", file=sys.stderr)
        return 1

    golden = load_golden()
    overrides = load_geometry_overrides(args.geometry)
    args.out.mkdir(parents=True, exist_ok=True)
    overlay_dir = args.out / "overlays"
    if args.overlays:
        overlay_dir.mkdir(parents=True, exist_ok=True)

    labels: list[dict[str, str]] = []
    skipped: list[str] = []

    for page_id in sorted(golden):
        matches = sorted(args.images.glob(f"{page_id}.*"))
        if not matches:
            skipped.append(f"{page_id}: no image in {args.images}")
            continue

        image = cv2.imread(str(matches[0]))
        if image is None:
            skipped.append(f"{page_id}: unreadable image {matches[0].name}")
            continue

        geometry = analyse(image)
        if page_id in overrides:
            geometry = apply_override(geometry, overrides[page_id])
        if geometry is None:
            skipped.append(f"{page_id}: no paper region found")
            continue

        if args.overlays:
            cv2.imwrite(str(overlay_dir / f"{page_id}.png"), draw_overlay(image, geometry))

        rows = golden[page_id]
        if len(geometry.rows) != len(rows) and not args.allow_row_mismatch:
            skipped.append(
                f"{page_id}: detected {len(geometry.rows)} rows, golden set has {len(rows)}"
                " -- crops would be mislabelled"
            )
            continue
        if len(geometry.columns) < 3:
            skipped.append(f"{page_id}: only {len(geometry.columns)} columns found")
            continue

        page_dir = args.out / page_id
        page_dir.mkdir(parents=True, exist_ok=True)

        for index, row in enumerate(rows):
            if index >= len(geometry.rows):
                break
            for column_index, column in enumerate(COLUMNS):
                cell = crop(image, geometry, index, column_index, pad=args.pad)
                if cell.size == 0:
                    continue
                name = f"{index:03d}_{column}.png"
                cv2.imwrite(str(page_dir / name), cell)
                labels.append(
                    {
                        "page_id": page_id,
                        "row_index": str(index),
                        "column": column,
                        "crop": f"{page_id}/{name}",
                        "expected": expected_value(row, column),
                    }
                )

    labels_path = args.out / "labels.csv"
    with labels_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(labels)

    pages_done = len({row["page_id"] for row in labels})
    print(f"wrote {len(labels)} labelled crops from {pages_done} pages to {args.out.relative_to(REPO_ROOT)}")
    if args.overlays:
        print(f"segmentation overlays in {overlay_dir.relative_to(REPO_ROOT)} -- check these before trusting the crops")
    if skipped:
        print(f"\nskipped {len(skipped)} page(s):", file=sys.stderr)
        for line in skipped:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nsupply correct geometry for these with --geometry (see the module docstring)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
