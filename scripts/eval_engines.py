"""Phase 0 bake-off: which recognizer is worth building on.

Scores every installed engine over the labelled crops produced by
scripts/make_crops.py and writes docs/engine_eval_report.md.

Four numbers per engine per field, because they answer different questions:

* **raw** -- exact match on the engine's own output. What the model can do.
* **corrected** -- exact match after the domain rules run. What the *system*
  can do, and the number that actually matters; a prefix whitelist and a
  fixed-width grammar recover a lot of what raw accuracy loses.
* **valid** -- share of readings that at least parse as a meter code. Tells you
  whether errors will be caught or shipped.
* **latency** -- p50 and p95 milliseconds per cell, on this machine's CPU.

Run this on the target Windows machine, on full-resolution images. Numbers from
the WhatsApp copies describe a resolution the system will not be used at.

Usage:
    python scripts/eval_engines.py
    python scripts/eval_engines.py --engines trocr_small,easyocr --fields meter
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ocr.correction import default_whitelist, parse_meter, repair_meter
from local_ocr.correction.char_confusion import coerce_digits
from local_ocr.correction.text_rules import collapse
from local_ocr.ocr.base import Field
from local_ocr.ocr.engines import REGISTRY, build
from local_ocr.paths import REPO_ROOT, SAMPLES_DIR  # noqa: E402

DEFAULT_CROPS = SAMPLES_DIR / "crops"
DEFAULT_REPORT = REPO_ROOT / "docs" / "engine_eval_report.md"

FIELD_BY_NAME = {"survey": Field.SURVEY, "meter": Field.METER, "remarks": Field.REMARKS}


@dataclass
class FieldScore:
    total: int = 0
    raw_hits: int = 0
    corrected_hits: int = 0
    valid: int = 0
    latencies: list[float] = field(default_factory=list)
    #: (crop, expected, raw, corrected) for the first few misses, for the report.
    examples: list[tuple[str, str, str, str]] = field(default_factory=list)

    def rate(self, hits: int) -> float:
        return 100.0 * hits / self.total if self.total else 0.0

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def apply_domain_rules(text: str, field_name: str) -> str:
    """Run the same post-correction the pipeline would, so the two numbers compare."""
    if field_name == "survey":
        digits = "".join(ch for ch in coerce_digits(text) if ch.isdigit())
        return digits
    if field_name == "meter":
        candidates = repair_meter(text, whitelist=default_whitelist())
        return candidates[0].text if candidates else text.upper()
    return collapse(text)


def load_labels(crops_dir: Path, fields: set[str]) -> list[dict[str, str]]:
    path = crops_dir / "labels.csv"
    if not path.exists():
        raise SystemExit(f"no labels at {path}; run scripts/make_crops.py first")
    with path.open(encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh) if row["column"] in fields]


def evaluate(engine, labels: list[dict[str, str]], crops_dir: Path) -> dict[str, FieldScore]:
    scores: dict[str, FieldScore] = defaultdict(FieldScore)

    for row in labels:
        expected = row["expected"].strip()
        # An empty cell tests detection, not recognition; skip for scoring.
        if not expected:
            continue

        image = cv2.imread(str(crops_dir / row["crop"]))
        if image is None:
            continue

        field_name = row["column"]
        result = engine.recognise(image, FIELD_BY_NAME[field_name])
        raw = result.text.strip()
        corrected = apply_domain_rules(raw, field_name)

        score = scores[field_name]
        score.total += 1
        score.latencies.append(result.latency_ms)
        if raw.upper() == expected.upper():
            score.raw_hits += 1
        if corrected.upper() == expected.upper():
            score.corrected_hits += 1
        else:
            if len(score.examples) < 12:
                score.examples.append((row["crop"], expected, raw, corrected))
        if field_name != "meter" or parse_meter(corrected) is not None:
            score.valid += 1

    return dict(scores)


def render_report(results: dict[str, dict[str, FieldScore]], crops_dir: Path, labels: list[dict]) -> str:
    lines = [
        "# Engine evaluation (Phase 0)",
        "",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- Crops: `{crops_dir}` ({len(labels)} labelled cells)",
        "- Ground truth: rows 596-882 of `SACHAL SURVEY - IMAGES DATA.xlsx`",
        "",
        "`corrected` is accuracy after the prefix whitelist and fixed-width grammar",
        "run; it is the number the system delivers. `valid` is the share of meter",
        "readings that parse at all -- a low `valid` with a high `corrected` means",
        "errors are being caught rather than shipped.",
        "",
    ]

    for field_name in ("meter", "survey", "remarks"):
        present = {name: r[field_name] for name, r in results.items() if field_name in r}
        if not present:
            continue
        lines += [
            f"## {field_name}",
            "",
            "| Engine | Cells | Raw | Corrected | Valid | p50 ms | p95 ms |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        ranked = sorted(present.items(), key=lambda kv: -kv[1].rate(kv[1].corrected_hits))
        for name, score in ranked:
            lines.append(
                f"| {name} | {score.total} | {score.rate(score.raw_hits):.1f}% | "
                f"**{score.rate(score.corrected_hits):.1f}%** | {score.rate(score.valid):.1f}% | "
                f"{score.p50:.0f} | {score.p95:.0f} |"
            )
        lines.append("")

    lines += ["## Sample misses", ""]
    for name, per_field in results.items():
        for field_name, score in per_field.items():
            if not score.examples:
                continue
            lines += [f"### {name} / {field_name}", "", "| Crop | Expected | Raw | Corrected |", "|---|---|---|---|"]
            for crop_name, expected, raw, corrected in score.examples:
                lines.append(f"| `{crop_name}` | `{expected}` | `{raw}` | `{corrected}` |")
            lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--crops", type=Path, default=DEFAULT_CROPS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--engines", default=",".join(REGISTRY))
    parser.add_argument("--fields", default="meter,survey,remarks")
    parser.add_argument("--limit", type=int, default=0, help="score only the first N cells per field")
    args = parser.parse_args(argv)

    fields = {f.strip() for f in args.fields.split(",") if f.strip()}
    labels = load_labels(args.crops, fields)
    if args.limit:
        capped: list[dict[str, str]] = []
        seen: dict[str, int] = defaultdict(int)
        for row in labels:
            if seen[row["column"]] < args.limit:
                capped.append(row)
                seen[row["column"]] += 1
        labels = capped

    print(f"{len(labels)} labelled cells across fields: {', '.join(sorted(fields))}")

    results: dict[str, dict[str, FieldScore]] = {}
    for name in [n.strip() for n in args.engines.split(",") if n.strip()]:
        try:
            engine = build(name)
        except KeyError as exc:
            print(f"  {exc}", file=sys.stderr)
            continue

        print(f"loading {name} ...", end=" ", flush=True)
        engine.ensure_loaded()
        if not engine.available:
            print(f"unavailable: {engine.unavailable_reason}")
            continue
        print("ok")

        started = time.perf_counter()
        results[engine.name] = evaluate(engine, labels, args.crops)
        elapsed = time.perf_counter() - started

        for field_name, score in sorted(results[engine.name].items()):
            print(
                f"  {field_name:8s} raw {score.rate(score.raw_hits):5.1f}%  "
                f"corrected {score.rate(score.corrected_hits):5.1f}%  "
                f"p50 {score.p50:5.0f}ms  p95 {score.p95:5.0f}ms  ({score.total} cells)"
            )
        print(f"  total {elapsed:.1f}s")

    if not results:
        print(
            "\nno engines available. Install at least one:\n"
            "  pip install torch --index-url https://download.pytorch.org/whl/cpu\n"
            "  pip install transformers      # TrOCR (handwriting)\n"
            "  pip install easyocr           # EasyOCR\n"
            "  pip install paddleocr paddlepaddle",
            file=sys.stderr,
        )
        return 1

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(results, args.crops, labels), encoding="utf-8")
    print(f"\nwrote {args.report.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
