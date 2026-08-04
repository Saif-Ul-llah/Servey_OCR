"""A first-pass page/row/column segmenter.

This is deliberately simple and is **not** the Phase 1 layout stage. It exists
so the Phase 0 engine bake-off has cells to run on, and so segmentation quality
is visible from day one via the debug overlay. The real stage adds
rule-line-anchored dewarping, blue-ink colour separation and bracket detection.

**Measured quality on the 12 WhatsApp-compressed samples (960x1280):**

* paper region found on 12/12,
* correct page of the spread chosen on 9/12,
* row counts wrong on most pages.

So it is not usable unsupervised. Two known causes, both fixed in Phase 1
rather than here:

1. the tan bag and concrete floor behind the notebook are bright and only
   moderately saturated, so they pass the paper test and inflate the region;
2. the gutter is found as the darkest vertical band, which the page's own
   shadow gradient beats -- on page_12 it lands in the middle of the meter
   column instead of at the fold.

Run ``python scripts/make_crops.py --overlays`` and look at the pictures before
trusting any of this; where it is wrong, supply geometry by hand.

It is also tuned against the wrong resolution -- glyphs are ~18px tall in the
compressed copies -- so expect to re-tune once the full-resolution originals are
in ``samples/images_hires/``. Every threshold is a named constant for that
reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

#: Paper is bright and unsaturated; ink, shadow, bag and floor are not.
PAPER_BRIGHTNESS_PERCENTILE = 55
PAPER_MAX_SATURATION = 70

#: Long horizontal runs in the threshold mask are the notebook's printed rules.
RULE_KERNEL_WIDTH = 45

#: A text band must be at least this tall, and carry this share of the busiest
#: row's ink, to count as a line of handwriting.
MIN_BAND_HEIGHT = 6
BAND_INK_THRESHOLD = 0.10

#: Where the three columns sit, as fractions of the target page's width.
#: Priors only -- the actual split points are found from the ink profile and
#: these bound the search.
COLUMN_PRIORS = ((0.00, 0.22), (0.22, 0.58), (0.58, 1.00))


@dataclass
class PageGeometry:
    """Where everything is on one photograph."""

    #: Bounding box of the paper (both pages of the spread).
    paper: tuple[int, int, int, int]
    #: Bounding box of the page we intend to transcribe.
    target: tuple[int, int, int, int]
    #: "left" or "right" -- which half of the spread that was.
    side: str
    #: x of the detected gutter, in full-frame coordinates.
    gutter_x: int
    #: (top, bottom) of each detected line of handwriting, target-page relative.
    rows: list[tuple[int, int]] = field(default_factory=list)
    #: (left, right) of each column, target-page relative.
    columns: list[tuple[int, int]] = field(default_factory=list)


def ink_mask(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Separate handwriting from paper, printed rules and bleed-through.

    Illumination is normalised by dividing out a heavy median blur, which
    flattens the shadow gradient across a curved page. The printed rules are
    then removed morphologically rather than by colour, because at WhatsApp
    resolution the blue ink's saturation collapses to roughly the rules' own.

    Returns ``(ink, rules)``; the rules mask is what the real dewarp stage will
    fit its warp field to.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    background = cv2.medianBlur(gray, 31)
    flattened = cv2.divide(gray, background, scale=255)

    binary = cv2.adaptiveThreshold(
        flattened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 18
    )
    rules = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (RULE_KERNEL_WIDTH, 1))
    )
    ink = cv2.subtract(binary, rules)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return ink, rules


def paper_region(image: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box of the open notebook, excluding the desk, bag and hands."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 1]

    paper = (
        (gray > np.percentile(gray, PAPER_BRIGHTNESS_PERCENTILE))
        & (saturation < PAPER_MAX_SATURATION)
    ).astype(np.uint8) * 255
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))

    count, _, stats, _ = cv2.connectedComponentsWithStats(paper, 8)
    if count <= 1:
        return None
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h = (int(v) for v in stats[largest, :4])
    return x, y, w, h


def find_gutter(image: np.ndarray, paper: tuple[int, int, int, int]) -> int:
    """x of the book's fold: the darkest vertical band near the middle."""
    x, y, w, h = paper
    strip = cv2.cvtColor(image[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
    darkness = strip.mean(axis=0)
    # Only search the middle half; the outer page edges are dark too.
    lo, hi = int(w * 0.25), int(w * 0.75)
    return x + lo + int(np.argmin(darkness[lo:hi]))


def choose_target_page(
    ink: np.ndarray, paper: tuple[int, int, int, int], gutter_x: int, frame_width: int
) -> tuple[tuple[int, int, int, int], str]:
    """Pick which half of the spread to transcribe.

    The photographer centres the page being recorded, so the facing page is
    usually clipped by the frame. Ink volume decides, with a penalty for a half
    that runs off the edge of the photograph.
    """
    x, y, w, h = paper
    halves = {
        "left": (x, y, max(1, gutter_x - x), h),
        "right": (gutter_x, y, max(1, x + w - gutter_x), h),
    }

    scores: dict[str, float] = {}
    for side, (hx, hy, hw, hh) in halves.items():
        region = ink[hy : hy + hh, hx : hx + hw]
        score = float((region > 0).sum()) / max(1, hw * hh)
        clipped = hx <= 2 or (hx + hw) >= frame_width - 2
        scores[side] = score * (0.5 if clipped else 1.0)

    side = max(scores, key=scores.get)
    return halves[side], side


def find_rows(ink: np.ndarray) -> list[tuple[int, int]]:
    """Horizontal bands of handwriting, top to bottom."""
    profile = (ink > 0).sum(axis=1)
    if profile.max() == 0:
        return []
    active = profile > profile.max() * BAND_INK_THRESHOLD

    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_active in enumerate(active):
        if is_active and start is None:
            start = index
        elif not is_active and start is not None:
            if index - start >= MIN_BAND_HEIGHT:
                bands.append((start, index))
            start = None
    if start is not None and len(active) - start >= MIN_BAND_HEIGHT:
        bands.append((start, len(active)))
    return bands


def find_columns(ink: np.ndarray) -> list[tuple[int, int]]:
    """Split the page into survey / meter / remarks bands by ink density.

    Uses `COLUMN_PRIORS` to bound the search so a long remark cannot drag the
    meter column's boundary across the page.
    """
    width = ink.shape[1]
    profile = (ink > 0).sum(axis=0).astype(float)
    if profile.max() == 0:
        return []
    profile = cv2.GaussianBlur(profile.reshape(1, -1), (0, 0), sigmaX=5).ravel()

    columns: list[tuple[int, int]] = []
    for low, high in COLUMN_PRIORS:
        lo, hi = int(width * low), int(width * high)
        window = profile[lo:hi]
        if window.max() <= 0:
            columns.append((lo, hi))
            continue
        active = np.nonzero(window > window.max() * 0.08)[0]
        columns.append((lo + int(active[0]), lo + int(active[-1]) + 1))
    return columns


def analyse(image: np.ndarray) -> PageGeometry | None:
    """Run the whole bootstrap segmentation over one photograph."""
    paper = paper_region(image)
    if paper is None:
        return None

    ink, _ = ink_mask(image)
    gutter_x = find_gutter(image, paper)
    target, side = choose_target_page(ink, paper, gutter_x, image.shape[1])

    tx, ty, tw, th = target
    target_ink = ink[ty : ty + th, tx : tx + tw]

    return PageGeometry(
        paper=paper,
        target=target,
        side=side,
        gutter_x=gutter_x,
        rows=find_rows(target_ink),
        columns=find_columns(target_ink),
    )


def draw_overlay(image: np.ndarray, geometry: PageGeometry) -> np.ndarray:
    """Render the segmentation for visual inspection.

    This is the main development instrument for the layout work: a wrong row or
    column is obvious here and invisible in an accuracy number.
    """
    canvas = image.copy()
    px, py, pw, ph = geometry.paper
    cv2.rectangle(canvas, (px, py), (px + pw, py + ph), (255, 128, 0), 2)

    tx, ty, tw, th = geometry.target
    cv2.rectangle(canvas, (tx, ty), (tx + tw, ty + th), (0, 200, 0), 2)
    cv2.line(canvas, (geometry.gutter_x, py), (geometry.gutter_x, py + ph), (0, 0, 255), 1)

    for index, (top, bottom) in enumerate(geometry.rows):
        cv2.rectangle(canvas, (tx, ty + top), (tx + tw, ty + bottom), (0, 255, 255), 1)
        cv2.putText(
            canvas, str(index), (max(0, tx - 26), ty + bottom),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA,
        )

    for left, right in geometry.columns:
        cv2.line(canvas, (tx + left, ty), (tx + left, ty + th), (255, 0, 255), 1)
        cv2.line(canvas, (tx + right, ty), (tx + right, ty + th), (255, 0, 255), 1)

    cv2.putText(
        canvas, f"{geometry.side} page | {len(geometry.rows)} rows",
        (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA,
    )
    return canvas


def crop(image: np.ndarray, geometry: PageGeometry, row: int, column: int, pad: int = 4) -> np.ndarray:
    """Cut one cell out of the original photograph."""
    tx, ty, _, _ = geometry.target
    top, bottom = geometry.rows[row]
    left, right = geometry.columns[column]
    y0 = max(0, ty + top - pad)
    y1 = min(image.shape[0], ty + bottom + pad)
    x0 = max(0, tx + left - pad)
    x1 = min(image.shape[1], tx + right + pad)
    return image[y0:y1, x0:x1]
