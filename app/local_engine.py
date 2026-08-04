"""Offline recognition with a local OCR engine.

This is the no-cloud counterpart to ``app/recognizer.py``. It runs the same
flow a batch job would: segment each page (``local_ocr.layout``), crop the three
columns per detected row, and read each cell with a local engine from
``local_ocr.ocr`` (TrOCR by default). The raw strings then feed the identical
correction -> assembly -> export core.

Two honest caveats, surfaced rather than hidden:

* recognition quality is capped by the segmenter, which is a bootstrap and is
  rough on the WhatsApp-compressed samples -- the review grid exists to catch
  its mistakes;
* the loaded model is cached process-wide, because building and loading TrOCR is
  the expensive part; a second run over more pages reuses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import importlib.util
import sys

from local_ocr.ocr.base import Engine, Field
from local_ocr.ocr.engines import REGISTRY, build

# OpenCV and the layout stage are imported lazily. The standalone .exe
# deliberately ships without them (bundling OpenCV and PyTorch would take it
# from 45 MB to gigabytes), and importing them at module scope would stop the
# whole server from starting there rather than just disabling one mode.
cv2 = None  # type: ignore[assignment]


def _load_layout():
    """Import the imaging stack on first use. Raises ImportError if absent."""
    global cv2
    import cv2 as _cv2

    from local_ocr.layout.bootstrap import crop
    from local_ocr.layout.ruled import masks, segment

    cv2 = _cv2
    return crop, masks, segment

#: Columns are emitted left-to-right by the segmenter in this fixed order.
_COLUMN_FIELDS = (Field.SURVEY, Field.METER, Field.REMARKS)

#: A cell holding less than this share of *handwriting* pixels is blank paper.
#: Rows are deliberately over-detected by the segmenter (a spare blank band is
#: free, a missed one loses a meter), so most cells on a page carry no writing.
#: Skipping them is the single largest speed win available, and it also removes
#: a source of invented values -- a model asked to read blank paper answers
#: anyway.
_BLANK_INK_SHARE = 0.004
#: Padding used by `crop`, mirrored here so the ink test looks at the same box.
_CROP_PAD = 4


def _cell_box(geometry, row: int, column: int, shape) -> tuple[int, int, int, int]:
    """The pixel box `crop` would cut, as ``(y0, y1, x0, x1)``."""
    tx, ty = geometry.target[0], geometry.target[1]
    top, bottom = geometry.rows[row]
    left, right = geometry.columns[column]
    y0 = max(0, ty + top - _CROP_PAD)
    y1 = min(shape[0], ty + bottom + _CROP_PAD)
    x0 = max(0, tx + left - _CROP_PAD)
    x1 = min(shape[1], tx + right + _CROP_PAD)
    return y0, y1, x0, x1


def _is_blank(ink: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """Ink test against the rule-subtracted mask.

    Thresholding the raw crop does not work: the printed ruling is darker than
    the paper, so every empty cell looks inked and almost nothing gets skipped.
    The mask from the layout stage has already removed the rules, so what is
    left is handwriting.
    """
    y0, y1, x0, x1 = box
    region = ink[y0:y1, x0:x1]
    if region.size == 0:
        return True
    return float((region > 0).sum()) / region.size < _BLANK_INK_SHARE

#: name -> loaded Engine. Loading a model takes seconds, so keep it around.
_ENGINE_CACHE: dict[str, Engine] = {}

#: Which third-party package each engine needs, for the availability check.
_ENGINE_PACKAGE = {
    "easyocr": "easyocr",
    "paddleocr": "paddleocr",
    "trocr_small": "transformers",
    "trocr_base": "transformers",
}


def availability(engine_name: str = "") -> tuple[bool, str]:
    """Can local OCR run here? Checked without importing or loading anything.

    Answered up front so the UI can say so plainly, rather than letting the user
    upload pages, press the button and wait for a failure.
    """
    if getattr(sys, "frozen", False):
        return False, (
            "Local OCR is not available in the standalone .exe — bundling OpenCV "
            "and the OCR engine would take it from 45 MB to gigabytes. "
            "Run the app with `python run_app.py` to use it, or pick a cloud mode."
        )
    if importlib.util.find_spec("cv2") is None:
        return False, "OpenCV is not installed (pip install opencv-python-headless)."
    package = _ENGINE_PACKAGE.get(engine_name or "", "")
    if package and importlib.util.find_spec(package) is None:
        return False, f"The '{engine_name}' engine needs `pip install {package}`."
    return True, ""


@dataclass
class LocalResult:
    page: str
    rows: list[dict] = field(default_factory=list)
    error: str | None = None


def known_engines() -> list[str]:
    return sorted(REGISTRY)


def get_engine(name: str) -> Engine:
    """Build and load an engine once, then reuse it. Raises KeyError if unknown."""
    engine = _ENGINE_CACHE.get(name)
    if engine is None:
        engine = build(name)  # KeyError for an unknown name -- caller handles it
        engine.ensure_loaded()
        _ENGINE_CACHE[name] = engine
    return engine


def recognise_page_local(path: Path | str, engine: Engine) -> LocalResult:
    """Segment one page and read every cell with ``engine``. Never raises."""
    page = Path(path).stem
    try:
        crop, masks, segment = _load_layout()
    except ImportError as exc:
        return LocalResult(page=page, error=f"local imaging stack unavailable: {exc}")

    image = cv2.imread(str(path))
    if image is None:
        return LocalResult(page=page, error="could not read image")

    geometry = segment(image)
    if geometry is None:
        return LocalResult(page=page, error="no notebook page detected")
    if len(geometry.columns) < len(_COLUMN_FIELDS):
        return LocalResult(page=page, error="could not locate the three columns")
    if not geometry.rows:
        return LocalResult(page=page, error="no rows of handwriting detected")

    ink, _ = masks(image)
    rows: list[dict] = []
    for row_index in range(len(geometry.rows)):
        text: dict[str, str] = {}
        conf: dict[str, float] = {}
        for column, field_kind in enumerate(_COLUMN_FIELDS):
            if _is_blank(ink, _cell_box(geometry, row_index, column, image.shape)):
                text[field_kind.value] = ""
                conf[field_kind.value] = 0.0
                continue
            cell_image = crop(image, geometry, row_index, column)
            recognition = engine.recognise(cell_image, field_kind)
            text[field_kind.value] = recognition.text
            conf[field_kind.value] = recognition.confidence
        rows.append(
            {
                "survey": text["survey"],
                "meter": text["meter"],
                "remarks": text["remarks"],
                "survey_conf": conf["survey"],
                "meter_conf": conf["meter"],
                "remarks_conf": conf["remarks"],
            }
        )
    return LocalResult(page=page, rows=rows)
