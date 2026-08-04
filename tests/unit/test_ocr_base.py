"""The engine interface, and the field-alphabet constraint applied to its output."""

from __future__ import annotations

import numpy as np
import pytest

from local_ocr.ocr import ALPHABETS, Field, Recognition, build, postprocess
from local_ocr.ocr.engines import REGISTRY


class TestFieldAlphabets:
    def test_survey_column_admits_digits_only(self):
        assert ALPHABETS[Field.SURVEY] == "0123456789"

    def test_remarks_are_unrestricted(self):
        assert ALPHABETS[Field.REMARKS] is None

    @pytest.mark.parametrize(
        "raw,field,expected",
        [
            ("KE 680", Field.SURVEY, "680"),      # letters cannot occur here
            ("6.8O", Field.SURVEY, "68"),
            ("sft 77430", Field.METER, "SFT77430"),
            ("SFT-77430!", Field.METER, "SFT77430"),
            ("  3   Shops ", Field.REMARKS, "3 Shops"),
        ],
    )
    def test_output_is_forced_into_the_field_alphabet(self, raw, field, expected):
        assert postprocess(raw, field) == expected

    def test_postprocess_handles_empty_output(self):
        assert postprocess("", Field.METER) == ""
        assert postprocess(None, Field.REMARKS) == ""


class TestRegistry:
    def test_every_registered_engine_constructs_without_its_backend(self):
        # Construction must never import the backend -- the bake-off has to run
        # with whatever subset is installed.
        for name in REGISTRY:
            engine = build(name)
            assert engine.name

    def test_unknown_engine_names_are_rejected_clearly(self):
        with pytest.raises(KeyError, match="unknown engine"):
            build("tesseract")

    def test_an_unavailable_engine_returns_an_empty_reading_not_a_crash(self):
        engine = build("trocr_small")
        engine.unavailable_reason = "simulated"
        result = engine.recognise(np.zeros((32, 128, 3), dtype=np.uint8), Field.METER)
        assert isinstance(result, Recognition)
        assert result.text == ""
        assert not result
