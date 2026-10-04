"""Shared, versioned semantic truth contract for knowledge and evaluation."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .semantic_ir import QueryPlan

SEMANTIC_SNAPSHOT_VERSION = 3
SEMANTIC_SNAPSHOT_KEYS = frozenset(
    {
        "version",
        "metrics",
        "metric_predicates",
        "dimensions",
        "entity_filters",
        "entity_filter_intents",
        "date_range",
        "granularity",
        "required_tables",
        "result_shape",
        "time_granularity",
        "comparison",
        "ambiguities",
        "status",
        "date_is_relative",
        "reference_date",
    }
)


class _SnapshotRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_min_length=1)


class _MetricPredicateSnapshot(_SnapshotRecord):
    metric: str
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq"]
    value: str = Field(pattern=r"^-?\d+(?:\.\d+)?$")


class _EntitySnapshot(_SnapshotRecord):
    entity: str
    table: str
    column: str
    values: list[str]
    operator: Literal["in", "not_in"]


class _DateRangeSnapshot(_SnapshotRecord):
    start: str | None
    end: str | None


class _ResultShapeSnapshot(_SnapshotRecord):
    sort_direction: Literal["asc", "desc"] | None
    sort_metric: str | None
    limit: int | None = Field(gt=0)
    distinct: bool


class SemanticExpectation(_SnapshotRecord):
    version: Literal[3]
    metrics: list[str]
    metric_predicates: list[_MetricPredicateSnapshot]
    dimensions: list[str]
    entity_filters: dict[str, list[str]]
    entity_filter_intents: list[_EntitySnapshot]
    date_range: _DateRangeSnapshot
    granularity: str
    required_tables: list[str]
    result_shape: _ResultShapeSnapshot
    time_granularity: Literal["day", "month", "quarter", "year"] | None
    comparison: str | None
    ambiguities: list[str]
    status: Literal["ready", "clarification_required", "unsupported"]
    date_is_relative: bool
    reference_date: str | None

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.date_is_relative != (self.reference_date is not None):
            raise ValueError("only relative calendar plans require a reference_date")
        for value in (self.date_range.start, self.date_range.end, self.reference_date):
            if value is not None:
                date.fromisoformat(value)
        if (self.date_range.start is None) != (self.date_range.end is None):
            raise ValueError("date_range must declare both boundaries or neither")
        if self.date_range.start and self.date_range.end:
            if self.date_range.start >= self.date_range.end:
                raise ValueError("date_range must be a nonempty half-open interval")
        return self


def validate_semantic_snapshot(payload: Any) -> list[str]:
    """Reject partial, obsolete or weakly typed semantic evaluation evidence."""
    try:
        SemanticExpectation.model_validate(payload)
    except ValidationError as exc:
        return [
            f"{'.'.join(map(str, item['loc'])) or 'snapshot'}: {item['msg']}"
            for item in exc.errors(include_url=False)
        ]
    return []


def build_semantic_snapshot(ir: QueryPlan) -> dict[str, Any]:
    """Return a stable representation without business-specific value remapping."""
    return {
        "version": SEMANTIC_SNAPSHOT_VERSION,
        "metrics": [item.name for item in ir.metrics],
        "metric_predicates": [
            {"metric": item.metric, "operator": item.operator, "value": item.value}
            for item in sorted(
                ir.metric_predicates, key=lambda item: (item.metric, item.operator, item.value)
            )
        ],
        "dimensions": list(ir.dimensions),
        "entity_filters": {
            name: list(values) for name, values in sorted(ir.entity_filters.items())
        },
        "entity_filter_intents": [
            {
                "entity": item.entity,
                "table": item.table,
                "column": item.column,
                "values": sorted(item.values),
                "operator": item.operator,
            }
            for item in sorted(
                ir.entity_filter_intents,
                key=lambda item: (
                    item.entity,
                    item.table,
                    item.column,
                    item.operator,
                    tuple(sorted(item.values)),
                ),
            )
        ],
        "date_range": {"start": ir.date_start, "end": ir.date_end},
        "granularity": ir.expected_granularity,
        "required_tables": list(ir.required_tables),
        "result_shape": {
            "sort_direction": ir.sort_direction,
            "sort_metric": ir.sort_metric,
            "limit": ir.limit,
            "distinct": ir.distinct,
        },
        "time_granularity": ir.time_granularity,
        "comparison": ir.comparison,
        "ambiguities": list(ir.ambiguities),
        "status": ir.status.value,
        "date_is_relative": ir.date_is_relative,
        "reference_date": ir.reference_date if ir.date_is_relative else None,
    }
