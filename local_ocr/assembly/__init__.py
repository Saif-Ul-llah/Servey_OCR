"""Batch-level assembly: page ordering, group healing, remark binding, audits."""

from local_ocr.assembly.audit import PLACEHOLDERS, audit, describe, survey_number
from local_ocr.assembly.batch import (
    AssemblyConfig,
    Batch,
    assemble,
    assign_groups,
    bind_remarks,
    order_pages,
    survey_range,
)

__all__ = [
    "AssemblyConfig",
    "Batch",
    "PLACEHOLDERS",
    "assemble",
    "assign_groups",
    "audit",
    "bind_remarks",
    "describe",
    "order_pages",
    "survey_number",
    "survey_range",
]
