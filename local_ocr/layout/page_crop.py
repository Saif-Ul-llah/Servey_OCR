"""Isolate the written notebook page from a survey photograph.

This is the reusable part of the layout work: a colour gate that finds the
paper and the ink cluster on it, so downstream stages (a VLM, or a review UI)
get a tight crop of just the page being transcribed instead of the whole
photo -- which is mostly desk, bag, fingers and the blank facing page.

The colour gate is calibrated from the WhatsApp samples (960x1280):

    paper : S ~ 9,  V ~ 197   (bright, near-zero saturation)
    bag   : S ~ 73            (tan)
    skin  : S ~ 140           (fingers)

so ``S < 40 & V > 160`` keeps paper and rejects both. Every threshold is a
named constant because these are fitted to compressed copies and will want a
re-check on higher-resolution originals.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

PAPER_MAX_SATURATION = 40
PAPER_MIN_VALUE = 160
#: Ink columns below this share of the busiest column are treated as blank.
INK_COLUMN_THRESHOLD = 0.12
INK_ROW_THRESHOLD = 0.06
#: Padding around the detected writing block, as a fraction of its size.
PAD_FRAC = 0.04


@dataclass
class Crop:
    """A page crop and where it came from in the original frame."""

    image: np.ndarray
    x: int
    y: int
    w: int
    h: int


def paper_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    mask = ((s < PAPER_MAX_SATURATION) & (v > PAPER_MIN_VALUE)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    return mask


def largest_component(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return None
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h = (int(v) for v in stats[largest, :4])
    return x, y, w, h


def _ink_mask(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    background = cv2.medianBlur(gray, 31)
    flattened = cv2.divide(gray, background, scale=255)
    binary = cv2.adaptiveThreshold(
        flattened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 18
    )
    rules = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (45, 1))
    )
    ink = cv2.subtract(binary, rules)
    return cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def find_page(image: np.ndarray, upscale: float = 2.0) -> Crop | None:
    """Return a tight, optionally upscaled crop of the written page.

    Paper is isolated by colour, ink is measured only on paper, and the crop is
    the bounding box of the writing block (which lies on whichever half of the
    spread was written on -- the blank facing page contributes no ink and is
    excluded automatically).
    """
    pmask = paper_mask(image)
    paper = largest_component(pmask)
    if paper is None:
        return None
    px, py, pw, ph = paper

    ink = cv2.bitwise_and(_ink_mask(image), pmask)
    ink_p = ink[py : py + ph, px : px + pw]

    col = cv2.GaussianBlur(
        (ink_p > 0).sum(axis=0).astype(float).reshape(1, -1), (0, 0), sigmaX=6
    ).ravel()
    row = (ink_p > 0).sum(axis=1).astype(float)
    if col.max() <= 0 or row.max() <= 0:
        return None

    xs = np.where(col > col.max() * INK_COLUMN_THRESHOLD)[0]
    ys = np.where(row > row.max() * INK_ROW_THRESHOLD)[0]
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())

    padx = int((x1 - x0) * PAD_FRAC)
    pady = int((y1 - y0) * PAD_FRAC)
    ax0 = max(0, px + x0 - padx)
    ay0 = max(0, py + y0 - pady)
    ax1 = min(image.shape[1], px + x1 + padx)
    ay1 = min(image.shape[0], py + y1 + pady)

    crop = image[ay0:ay1, ax0:ax1]
    if upscale and upscale != 1.0:
        crop = cv2.resize(crop, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    return Crop(image=crop, x=ax0, y=ay0, w=ax1 - ax0, h=ay1 - ay0)
