"""Versioned, canonical Semantic IR projection used by evaluation datasets."""

from __future__ import annotations

from typing import Any

from ..domain.semantic_ir import QuestionSemanticIR

SEMANTIC_SNAPSHOT_VERSION = 2


def build_semantic_snapshot(ir: QuestionSemanticIR) -> dict[str, Any]:
    """Return a stable representation without business-specific value remapping."""
    return {
        "version": SEMANTIC_SNAPSHOT_VERSION,
        "metrics": [item.name for item in ir.metrics],
        "dimensions": list(ir.dimensions),
        "entity_filters": {
            name: list(values) for name, values in sorted(ir.entity_filters.items())
        },
        "date_range": {"start": ir.date_start, "end": ir.date_end},
        "granularity": ir.expected_granularity,
        "required_tables": list(ir.required_tables),
        "result_shape": {
            "sort_direction": ir.sort_direction,
            "limit": ir.limit,
            "distinct": ir.distinct,
        },
        "time_granularity": ir.time_granularity,
        "comparison": ir.comparison,
        "ambiguities": list(ir.ambiguities),
    }
