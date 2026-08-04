"""Generate config/meter_prefixes.txt from the reference workbook.

The prefix whitelist is the single most valuable constraint in the pipeline: it
turns reading a meter prefix from open-vocabulary OCR into a closed-set
classification. It must therefore be *derived*, not hand-typed -- an earlier
hand-written list in LOCAL_OCR_IMPLEMENTATION_PLAN.md was missing 20 prefixes
that occur in the real data, every one of which would have produced a false
"invalid_prefix" flag at runtime.

Usage:
    python scripts/derive_prefixes.py
    python scripts/derive_prefixes.py --workbook path/to/ref.xlsx --out config/meter_prefixes.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKBOOK = REPO_ROOT / "Samples or examle data" / "SACHAL SURVEY - IMAGES DATA.xlsx"
DEFAULT_OUT = REPO_ROOT / "config" / "meter_prefixes.txt"

METER_RE = re.compile(r"^([A-Z]{2,3})(\d{4,6})$")


def read_meter_column(workbook: Path) -> list[str]:
    wb = openpyxl.load_workbook(workbook, data_only=True, read_only=True)
    ws = wb.active
    values: list[str] = []
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=2, values_only=True):
        if row[0] is not None:
            values.append(str(row[0]).strip())
    wb.close()
    return values


def split_meters(values: list[str]) -> tuple[Counter, Counter, list[str]]:
    """Return (prefix counts, digit-length counts, values that are not meter codes)."""
    prefixes: Counter = Counter()
    digit_lengths: Counter = Counter()
    anomalies: list[str] = []
    for value in values:
        match = METER_RE.match(value)
        if match:
            prefixes[match.group(1)] += 1
            digit_lengths[len(match.group(2))] += 1
        else:
            anomalies.append(value)
    return prefixes, digit_lengths, anomalies


def write_whitelist(prefixes: Counter, out_path: Path, source: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Meter-code prefix whitelist -- GENERATED FILE, do not hand-edit.",
        "#",
        f"# Source:    {source.name}",
        "# Regenerate: python scripts/derive_prefixes.py",
        "#",
        "# Format: PREFIX<TAB>observed_count",
        "# The count lets the fuzzy matcher prefer common prefixes when two",
        "# candidates score equally. To add a prefix seen in the field but absent",
        "# here, append it with a count of 0 and note it in docs/adding_prefixes.md.",
        "",
    ]
    # Sort by prefix so diffs between regenerations are readable.
    for prefix, count in sorted(prefixes.items()):
        lines.append(f"{prefix}\t{count}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.workbook.exists():
        print(f"error: workbook not found: {args.workbook}", file=sys.stderr)
        return 1

    values = read_meter_column(args.workbook)
    prefixes, digit_lengths, anomalies = split_meters(values)
    write_whitelist(prefixes, args.out, args.workbook)

    print(f"read {len(values)} meter values from {args.workbook.name}")
    print(f"wrote {len(prefixes)} prefixes to {args.out.relative_to(REPO_ROOT)}")
    print()
    print("digit-length distribution:")
    for length, count in sorted(digit_lengths.items()):
        print(f"  {length} digits: {count}")
    print()
    print(f"values that are not meter codes ({len(anomalies)}):")
    for value, count in sorted(Counter(anomalies).items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3} x {value!r}")
    print()
    print("prefixes seen fewer than 3 times (most likely to be transcription slips):")
    rare = [(p, c) for p, c in sorted(prefixes.items()) if c < 3]
    for prefix, count in rare:
        print(f"  {prefix} ({count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
