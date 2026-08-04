"""The recognizer interface every engine implements.

Engines are told *which field* they are reading, because the three columns have
very different alphabets: the survey column holds digits only, meter codes hold
uppercase letters and digits, and remarks are free English text. Handing that
information to the engine is the cheapest accuracy win available -- an engine
that cannot emit a letter into a digits-only field cannot make that mistake.

Every adapter is import-guarded. A missing package makes an engine unavailable,
never an import error, so the Phase 0 bake-off can run with whatever subset is
installed.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field as dataclass_field
from enum import Enum

import numpy as np


class Field(str, Enum):
    SURVEY = "survey"
    METER = "meter"
    REMARKS = "remarks"


#: Characters each field may contain. `None` means unrestricted.
ALPHABETS: dict[Field, str | None] = {
    Field.SURVEY: "0123456789",
    Field.METER: "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    Field.REMARKS: None,
}


@dataclass
class Recognition:
    """One engine's reading of one cell."""

    text: str
    confidence: float
    engine: str
    latency_ms: float = 0.0
    #: Alternative decodings, best first, when the engine can supply them.
    alternatives: list[str] = dataclass_field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text.strip())


class Engine(ABC):
    """Base class for a handwriting recognizer.

    Models are loaded lazily on first use so that constructing an engine is free
    and a worker process only pays for the engine it actually runs.
    """

    name: str = "base"
    #: Set by the adapter when its backing package could not be imported.
    unavailable_reason: str | None = None

    def __init__(self) -> None:
        self._loaded = False

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()
            self._loaded = True

    @abstractmethod
    def _load(self) -> None:
        """Bring the model into memory. Called once, lazily."""

    @abstractmethod
    def _recognise(self, image: np.ndarray, field: Field) -> Recognition:
        """Read one cell crop. Implementations need not time themselves."""

    def recognise(self, image: np.ndarray, field: Field = Field.METER) -> Recognition:
        """Read one cell crop, with timing attached."""
        if not self.available:
            return Recognition(text="", confidence=0.0, engine=self.name)
        self.ensure_loaded()
        started = time.perf_counter()
        result = self._recognise(image, field)
        result.latency_ms = (time.perf_counter() - started) * 1000
        result.engine = self.name
        return result

    def recognise_many(
        self, images: list[np.ndarray], field: Field = Field.METER
    ) -> list[Recognition]:
        """Read several crops. Override when the backend batches efficiently."""
        return [self.recognise(image, field) for image in images]


def postprocess(text: str, field: Field) -> str:
    """Drop characters the field cannot contain and normalise case.

    Applied to every engine's output so that adapters which cannot be given an
    alphabet up front still benefit from the constraint.
    """
    if field is Field.REMARKS:
        return " ".join((text or "").split())

    allowed = ALPHABETS[field]
    upper = (text or "").upper()
    return "".join(char for char in upper if allowed is None or char in allowed)
