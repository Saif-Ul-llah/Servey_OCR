"""Rule-line anchored page segmentation.

The bootstrap segmenter in ``bootstrap.py`` keys off *ink*: it finds the paper by
brightness, the fold by darkness, and the rows by horizontal bands of writing.
All three fail on real photographs -- the tan bag behind the notebook is bright
and unsaturated, the page's own shadow gradient is darker than the fold, and two
cramped lines of handwriting merge into one ink band while a tall one splits.

This module keys off *structure* instead. The notebook is ruled, and those
printed lines are the most reliable thing in the frame:

* **Paper**: only real paper carries ruled lines, so bright candidate regions are
  scored by the density of horizontal rules inside them. The bag scores zero.
* **Gutter**: the rules run across each page and stop at the fold, so the column
  profile of the rule mask has a pronounced trough exactly at the gutter -- a far
  stronger signal than "darkest column".
* **Rows**: a row *is* the band between two consecutive printed rules. Counting
  rules rather than ink bands makes the row count independent of how much the
  surveyor wrote, which is what the ink-band approach gets wrong.

Row geometry therefore comes from the page's printing, and the recogniser is
handed the same boxes the surveyor was writing into.
"""

from __future__ import annotations

import cv2
import numpy as np

from local_ocr.layout.bootstrap import PageGeometry

#: Width of the opening kernel that removes vertical pen strokes. Deliberately
#: short: the pages are photographed at a slight angle, so a rule drifts
#: vertically across its length and a long straight kernel misses it entirely.
RULE_OPEN_KERNEL = 21
#: A rule fragment must be at least this wide (fraction of the region searched)
#: and no taller than `RULE_MAX_HEIGHT`. Tilt breaks a rule into fragments; each
#: fragment is still wide and thin, which handwriting is not.
RULE_MIN_WIDTH_FRACTION = 0.12
RULE_MAX_HEIGHT = 6
#: Rules closer together than this (as a fraction of the median pitch) are the
#: same physical line detected twice.
RULE_MERGE_FRACTION = 0.45
#: Ruling is evenly spaced, so a gap of ~k pitches means k-1 rules were missed.
#: Gaps up to this many pitches are filled in; beyond that the page is too
#: damaged to guess and the gap is left alone.
RULE_MAX_GAP_FILL = 6
#: A band must hold at least this share of the busiest band's ink to be a row.
#: Deliberately low, because the two failure directions are not symmetric: an
#: extra blank band costs nothing (assembly drops rows with no ink in any
#: column), while a missed band silently loses a real meter reading. Measured on
#: the golden set, raising this trades a lower mean row-count error for exactly
#: that silent loss, so it stays low on purpose.
ROW_INK_THRESHOLD = 0.08
#: Paper is bright and barely saturated. Kept loose: the rule-density score,
#: not these thresholds, is what rejects the bag.
PAPER_BRIGHTNESS_PERCENTILE = 50
PAPER_MAX_SATURATION = 90
#: A candidate region must be at least this share of the frame to be the paper.
PAPER_MIN_COVERAGE = 0.12
#: Where the three columns sit, as fractions of the written block's width.
#: Priors only -- the exact splits are found from the ink profile inside these
#: windows. The block is cropped to the writing, so these are generous.
COLUMN_PRIORS = ((0.00, 0.30), (0.18, 0.72), (0.62, 1.00))
#: A column carrying at least this share of the busiest column's ink is writing.
INK_COLUMN_THRESHOLD = 0.05
#: Blank bands narrower than this (fraction of paper width) are the spaces
#: between a page's own columns; anything wider is the fold.
INK_GAP_BRIDGE_FRACTION = 0.055
#: Breathing room added around the detected block, so descenders survive.
TARGET_PAD_FRACTION = 0.015
#: Column detection: a column of ink must clear this share of the busiest column
#: and be at least this wide, so a stray speck is not mistaken for a column.
COLUMN_INK_THRESHOLD = 0.04
COLUMN_MIN_WIDTH_FRACTION = 0.02
#: Minimum share of the page's ink each column must carry. Asymmetric on
#: purpose: the meter column is always dense, the remarks column often holds a
#: single word, and the survey column only carries a number on a group's first
#: row. Measured on the golden set.
COLUMN_MIN_SHARE_SURVEY = 0.06
COLUMN_MIN_SHARE_METER = 0.15
COLUMN_MIN_SHARE_REMARKS = 0.003


def flatten_illumination(image: np.ndarray) -> np.ndarray:
    """Divide out a heavy blur so a curved, unevenly lit page reads flat."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    background = cv2.medianBlur(gray, 31)
    return cv2.divide(gray, background, scale=255)


def _wide_thin(mask: np.ndarray, min_width: int) -> tuple[np.ndarray, list[float]]:
    """Keep only wide, thin blobs. Returns ``(mask, centre y of each)``.

    This is what makes tilt survivable: a sloping rule is broken into fragments
    by the opening, but every fragment is still far wider than it is tall, which
    no piece of handwriting is.
    """
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    centres: list[float] = []
    for index in range(1, count):
        width = int(stats[index, cv2.CC_STAT_WIDTH])
        height = int(stats[index, cv2.CC_STAT_HEIGHT])
        if width >= min_width and height <= RULE_MAX_HEIGHT:
            keep[labels == index] = 255
            centres.append(float(centroids[index][1]))
    return keep, sorted(centres)


def masks(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(ink, rules)`` for the whole frame.

    ``rules`` keeps the printed ruling only; ``ink`` is everything else -- the
    handwriting, plus whatever noise survived flattening.
    """
    flattened = flatten_illumination(image)
    binary = cv2.adaptiveThreshold(
        flattened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 18
    )
    opened = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (RULE_OPEN_KERNEL, 1))
    )
    rules, _ = _wide_thin(opened, int(image.shape[1] * RULE_MIN_WIDTH_FRACTION))
    # Subtract every horizontal run, not just the confirmed rules, so a rule the
    # filter rejected cannot masquerade as handwriting.
    ink = cv2.subtract(binary, opened)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return ink, rules


def paper_region(image: np.ndarray, rules: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box of the ruled notebook, chosen by rule density.

    Several bright, unsaturated blobs usually survive thresholding (the page, the
    bag, a patch of floor). The page is the one with printed lines in it, so
    candidates are ranked by rule pixels per unit area rather than by size.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 1]

    bright = (
        (gray > np.percentile(gray, PAPER_BRIGHTNESS_PERCENTILE))
        & (saturation < PAPER_MAX_SATURATION)
    ).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))

    count, _, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    if count <= 1:
        return None

    frame_area = image.shape[0] * image.shape[1]
    best: tuple[float, tuple[int, int, int, int]] | None = None
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index, :5])
        if area < frame_area * PAPER_MIN_COVERAGE:
            continue
        region = rules[y : y + h, x : x + w]
        density = float((region > 0).sum()) / max(1, w * h)
        if best is None or density > best[0]:
            best = (density, (x, y, w, h))

    if best is None:
        # Nothing was big enough; fall back to the largest component so the
        # caller still gets geometry rather than nothing.
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, w, h = (int(v) for v in stats[largest, :4])
        return x, y, w, h
    return best[1]


def text_block(
    ink: np.ndarray, paper: tuple[int, int, int, int]
) -> tuple[tuple[int, int, int, int], str]:
    """Find the written page by clustering ink columns. Returns ``(box, side)``.

    Hunting for the fold directly does not work. The obvious signals both lie:
    the fold is not the darkest column (the page's own shadow gradient is), and
    it is not the biggest break in the ruling either, because handwriting sitting
    on top of the rules breaks *them* up and manufactures a deeper trough right
    in the middle of the text. Measured on the golden set, that put the split
    straight through the meter column and handed the recogniser half-codes.

    So this looks for the writing rather than the fold. Ink is projected onto the
    x axis and the columns that carry it are grouped into blocks; the fold is
    simply the blank band between two blocks, and a blank facing page is no block
    at all. The densest block is the page being transcribed. Small gaps -- the
    space between the three columns -- are bridged, so a page stays one block.
    """
    x, y, w, h = paper
    strip = ink[y : y + h, x : x + w]
    profile = (strip > 0).sum(axis=0).astype(float)
    if profile.max() <= 0:
        return paper, "left"

    profile = cv2.GaussianBlur(profile.reshape(1, -1), (0, 0), sigmaX=max(1.5, w * 0.004)).ravel()
    active = profile > profile.max() * INK_COLUMN_THRESHOLD

    runs: list[list[int]] = []
    start: int | None = None
    for index, is_active in enumerate(active):
        if is_active and start is None:
            start = index
        elif not is_active and start is not None:
            runs.append([start, index])
            start = None
    if start is not None:
        runs.append([start, len(active)])
    if not runs:
        return paper, "left"

    # Bridge the gaps *within* a page (between its three columns) but not the
    # wide blank band at the fold.
    bridge = int(w * INK_GAP_BRIDGE_FRACTION)
    merged = [runs[0]]
    for run in runs[1:]:
        if run[0] - merged[-1][1] <= bridge:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    best = max(merged, key=lambda run: float(profile[run[0] : run[1]].sum()))
    pad = int(w * TARGET_PAD_FRACTION)
    left = max(0, best[0] - pad)
    right = min(w, best[1] + pad)
    box = (x + left, y, max(1, right - left), h)
    side = "left" if (left + right) / 2 < w / 2 else "right"
    return box, side


def find_gutter(ink: np.ndarray, paper: tuple[int, int, int, int]) -> int:
    """x of the fold, inferred as the edge of the written block facing the spine."""
    (bx, _, bw, _), side = text_block(ink, paper)
    return bx if side == "right" else bx + bw


def rule_positions(image: np.ndarray, target: tuple[int, int, int, int]) -> list[int]:
    """y of each printed rule inside the target page, top to bottom.

    Detection is re-run on the target page rather than reusing the frame-wide
    mask, because "wide" only means something relative to the page being read.
    The result is then regularised: ruling is evenly spaced, so the median pitch
    reveals -- and fills -- lines the threshold missed. That regular grid is what
    makes the row count independent of how much was written.
    """
    tx, ty, tw, th = target
    crop = image[ty : ty + th, tx : tx + tw]
    if crop.size == 0:
        return []

    flattened = flatten_illumination(crop)
    binary = cv2.adaptiveThreshold(
        flattened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 18
    )
    opened = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (RULE_OPEN_KERNEL, 1))
    )
    _, centres = _wide_thin(opened, int(tw * RULE_MIN_WIDTH_FRACTION))
    if len(centres) < 2:
        return [int(c) for c in centres]

    # One physical line can yield several fragments at nearly the same height.
    pitch = float(np.median(np.diff(centres))) or 1.0
    merged: list[float] = [centres[0]]
    for centre in centres[1:]:
        if centre - merged[-1] < max(3.0, pitch * RULE_MERGE_FRACTION):
            merged[-1] = (merged[-1] + centre) / 2
        else:
            merged.append(centre)
    if len(merged) < 3:
        return [int(c) for c in merged]

    return _regularise(merged)


def _regularise(lines: list[float]) -> list[int]:
    """Fill rules missed by the threshold, using the ruling's constant pitch."""
    gaps = np.diff(lines)
    pitch = float(np.median(gaps))
    if pitch <= 0:
        return [int(line) for line in lines]

    filled: list[float] = [lines[0]]
    for previous, current in zip(lines, lines[1:]):
        span = current - previous
        steps = int(round(span / pitch))
        if 2 <= steps <= RULE_MAX_GAP_FILL:
            for step in range(1, steps):
                filled.append(previous + pitch * step)
        filled.append(current)
    return [int(round(line)) for line in filled]


def _phase_align(page_ink: np.ndarray, lines: list[int], pitch: float, height: int) -> list[int]:
    """Slide the rule grid so band boundaries fall between lines of writing.

    The surveyor writes *on* the ruled line, so the glyphs straddle it: a band
    running rule-to-rule cuts every entry in half and the same code is then read
    twice, once from each side. The grid therefore gets shifted as a whole to
    whichever phase crosses the least ink -- the same argument used for the
    column boundaries, applied to the other axis.
    """
    profile = (page_ink > 0).sum(axis=1).astype(float)
    if profile.max() <= 0:
        return lines

    best_offset, best_cost = 0, None
    for offset in range(-int(pitch), 1):
        cost = 0.0
        for line in lines:
            position = line + offset
            if 0 <= position < height:
                cost += float(profile[position])
        if best_cost is None or cost < best_cost:
            best_offset, best_cost = offset, cost

    shifted = [max(0, min(height, line + best_offset)) for line in lines]
    # Keep a band at the bottom that the shift would otherwise walk off.
    if shifted and shifted[-1] + pitch <= height:
        shifted.append(int(shifted[-1] + pitch))
    return shifted


def rows_between_rules(
    ink: np.ndarray, target: tuple[int, int, int, int], lines: list[int]
) -> list[tuple[int, int]]:
    """Rows are the bands between consecutive rules that contain handwriting.

    The grid is extended one pitch beyond the first and last detected rule: the
    topmost and bottommost written lines often sit against a rule that the
    threshold missed, and dropping them would silently lose a row.
    """
    tx, ty, tw, th = target
    page_ink = ink[ty : ty + th, tx : tx + tw]
    if len(lines) < 2:
        return []

    pitch = float(np.median(np.diff(lines)))
    if pitch > 0:
        lines = (
            [max(0, int(lines[0] - pitch))]
            + list(lines)
            + [min(th, int(lines[-1] + pitch))]
        )
        lines = _phase_align(page_ink, lines, pitch, th)

    bands = [(lines[i], lines[i + 1]) for i in range(len(lines) - 1)]
    scores = []
    for top, bottom in bands:
        # Ignore the rule pixels themselves at the band edges.
        inner_top, inner_bottom = min(top + 1, bottom), max(bottom - 1, top + 1)
        region = page_ink[inner_top:inner_bottom]
        scores.append(float((region > 0).sum()))

    peak = max(scores) if scores else 0.0
    if peak <= 0:
        return []
    return [band for band, score in zip(bands, scores) if score >= peak * ROW_INK_THRESHOLD]


def find_columns(ink: np.ndarray, target: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """Locate the survey / meter / remarks bands from the ink column profile.

    Fixed proportional windows do not survive contact with a real page: how far
    the meter column sits from the survey number varies with the surveyor's hand,
    and a page whose remarks are nearly empty shifts every boundary. So the
    columns are *found* -- ink is projected onto the x axis, the runs that carry
    it are collected, and adjacent runs are merged across the narrowest gaps
    until three groups remain. The two surviving gaps are, by construction, the
    widest blank channels on the page, which is exactly what separates the
    columns.

    A page with an empty remarks column yields two groups; the remainder of the
    block to the right becomes an empty third column, so downstream code always
    sees three.
    """
    tx, ty, tw, th = target
    page_ink = ink[ty : ty + th, tx : tx + tw]
    profile = (page_ink > 0).sum(axis=0).astype(float)
    fallback = [(int(tw * lo), int(tw * hi)) for lo, hi in COLUMN_PRIORS]
    if profile.max() <= 0:
        return fallback

    profile = cv2.GaussianBlur(
        profile.reshape(1, -1), (0, 0), sigmaX=max(2.0, tw * 0.012)
    ).ravel()

    # Thresholding cannot find both boundaries at once: the meter column is dense
    # and the remarks column is often nearly empty, so any single cut-off either
    # keeps the columns joined or erases the remarks. The boundaries are instead
    # chosen as the pair of *valleys* that minimises ink crossed, subject to each
    # resulting column holding real ink -- which stops a margin or the shadowed
    # page edge from being taken for a column.
    total = float(profile.sum()) or 1.0
    cumulative = np.concatenate([[0.0], np.cumsum(profile)])

    def share(lo: int, hi: int) -> float:
        return float(cumulative[hi] - cumulative[lo]) / total

    margin = max(4, int(tw * 0.06))
    separation = max(6, int(tw * 0.08))
    window = max(6, int(tw * 0.12))
    candidates = list(range(margin, tw - margin, max(1, tw // 200)))

    def valley(position: int) -> float:
        """How much of a trough this position is: 0 is a clean gap, 1 is flat.

        Scoring the raw ink instead would put both splits inside the *emptiest*
        stretch of the page -- usually the blank right-hand margin -- because
        nothing there costs anything to cut. Measuring the dip against the
        columns on either side is what makes a boundary between two dense
        columns beat an arbitrary cut through white space.
        """
        left_peak = float(profile[max(0, position - window) : position].max(initial=0.0))
        right_peak = float(profile[position : min(tw, position + window)].max(initial=0.0))
        # Measured against the *strongest* neighbouring column, not the weakest.
        # Against the weakest, the real boundary before a near-empty remarks
        # column scores worse than a chance dip between two digit groups inside
        # the meter column, and the meter codes get cut in half.
        flank = max(left_peak, right_peak)
        if flank <= 0:
            return 1.0
        return float(profile[position]) / flank

    # Each column must carry ink of its own, and they are not alike: the meter
    # column is the reason the notebook exists and is always the densest, while
    # the remarks column is frequently near-empty. Holding the middle group to a
    # real share is what stops a sliver of the gap itself being called a column.
    best: tuple[float, int, int] | None = None
    for i, first in enumerate(candidates):
        if share(0, first) < COLUMN_MIN_SHARE_SURVEY:
            continue
        first_cost = valley(first)
        for second in candidates[i + 1 :]:
            if second - first < separation:
                continue
            if share(first, second) < COLUMN_MIN_SHARE_METER:
                continue
            if share(second, tw) < COLUMN_MIN_SHARE_REMARKS:
                continue
            cost = first_cost + valley(second)
            if best is None or cost < best[0]:
                best = (cost, first, second)

    if best is None:
        return fallback
    _, first, second = best
    return [(0, first), (first, second), (second, tw)]


def segment(image: np.ndarray) -> PageGeometry | None:
    """Full rule-anchored segmentation of one photograph."""
    ink, rules = masks(image)
    paper = paper_region(image, rules)
    if paper is None:
        return None

    target, side = text_block(ink, paper)
    gutter_x = target[0] if side == "right" else target[0] + target[2]
    lines = rule_positions(image, target)
    rows = rows_between_rules(ink, target, lines)

    return PageGeometry(
        paper=paper,
        target=target,
        side=side,
        gutter_x=gutter_x,
        rows=rows,
        columns=find_columns(ink, target),
        detected_rules=len(lines),
    )
