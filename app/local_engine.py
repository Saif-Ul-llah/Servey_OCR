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

import cv2

from local_ocr.layout.bootstrap import analyse, crop
from local_ocr.ocr.base import Engine, Field
from local_ocr.ocr.engines import REGISTRY, build

#: Columns are emitted left-to-right by the segmenter in this fixed order.
_COLUMN_FIELDS = (Field.SURVEY, Field.METER, Field.REMARKS)

#: name -> loaded Engine. Loading TrOCR takes seconds, so keep it around.
_ENGINE_CACHE: dict[str, Engine] = {}


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
    image = cv2.imread(str(path))
    if image is None:
        return LocalResult(page=page, error="could not read image")

    geometry = analyse(image)
    if geometry is None:
        return LocalResult(page=page, error="no notebook page detected")
    if len(geometry.columns) < len(_COLUMN_FIELDS):
        return LocalResult(page=page, error="could not locate the three columns")
    if not geometry.rows:
        return LocalResult(page=page, error="no rows of handwriting detected")

    rows: list[dict] = []
    for row_index in range(len(geometry.rows)):
        text: dict[str, str] = {}
        conf: dict[str, float] = {}
        for column, field_kind in enumerate(_COLUMN_FIELDS):
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
