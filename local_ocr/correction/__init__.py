"""Domain post-correction: grammar, whitelist, repeat marks, placeholders, remarks."""

from local_ocr.correction.char_confusion import (
    MeterCandidate,
    coerce_digits,
    digit_alternatives,
    repair_meter,
)
from local_ocr.correction.ditto import (
    DittoContext,
    expand_meter_ditto,
    expand_remarks_ditto,
    looks_like_meter_ditto,
)
from local_ocr.correction.glyphs import has_pointer, resolve_corrected_digits
from local_ocr.correction.pipeline import CorrectionConfig, Corrector
from local_ocr.correction.text_rules import (
    extract_annotations,
    format_remarks,
    is_ditto,
    is_section_marker,
    load_section_markers,
    strip_non_latin,
)
from local_ocr.correction.tokens import SpecialTokens
from local_ocr.correction.validator import (
    Meter,
    PrefixWhitelist,
    default_whitelist,
    is_valid_meter,
    normalise,
    parse_meter,
)

__all__ = [
    "CorrectionConfig",
    "Corrector",
    "DittoContext",
    "Meter",
    "MeterCandidate",
    "PrefixWhitelist",
    "SpecialTokens",
    "coerce_digits",
    "default_whitelist",
    "digit_alternatives",
    "expand_meter_ditto",
    "expand_remarks_ditto",
    "extract_annotations",
    "format_remarks",
    "has_pointer",
    "is_ditto",
    "is_section_marker",
    "is_valid_meter",
    "load_section_markers",
    "looks_like_meter_ditto",
    "normalise",
    "parse_meter",
    "repair_meter",
    "resolve_corrected_digits",
    "strip_non_latin",
]
