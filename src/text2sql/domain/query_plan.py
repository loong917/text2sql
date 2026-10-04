"""Typed, catalog-grounded contract between question parsing and SQL generation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import Any, Literal


class PlanStatus(StrEnum):
    READY = "ready"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class MetricIntent:
    name: str
    aggregate: str
    column: str = "*"
    output_alias: str = "Value"
    source_table: str = ""


@dataclass(frozen=True)
class EntityFilterIntent:
    entity: str
    table: str
    column: str
    values: tuple[str, ...]
    operator: Literal["in", "not_in"] = "in"


@dataclass(frozen=True)
class MetricPredicate:
    metric: str
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq"]
    value: str


@dataclass(frozen=True)
class JoinIntent:
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    relationship: str = ""


@dataclass(frozen=True)
class SemanticCatalog:
    metrics: tuple[dict[str, Any], ...] = ()
    dimensions: tuple[dict[str, Any], ...] = ()
    entity_policies: tuple[dict[str, Any], ...] = ()
    joins: tuple[dict[str, Any], ...] = ()
    entities: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class QueryPlan:
    original_question: str
    normalized_question: str
    metrics: tuple[MetricIntent, ...] = ()
    dimensions: tuple[str, ...] = ()
    entity_filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    entity_filter_intents: tuple[EntityFilterIntent, ...] = ()
    date_start: str | None = None
    date_end: str | None = None
    expected_granularity: str = "aggregate"
    required_tables: tuple[str, ...] = ()
    required_joins: tuple[JoinIntent, ...] = ()
    dimension_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dimension_tables: dict[str, str] = field(default_factory=dict)
    dimension_output_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    date_table: str | None = None
    date_column: str | None = None
    time_granularity: Literal["day", "month", "quarter", "year"] | None = None
    sort_direction: Literal["asc", "desc"] | None = None
    sort_metric: str | None = None
    limit: int | None = None
    distinct: bool = False
    comparison: str | None = None
    metric_predicates: tuple[MetricPredicate, ...] = ()
    ambiguities: tuple[str, ...] = ()
    status: PlanStatus = PlanStatus.READY
    diagnostics: tuple[str, ...] = ()
    reference_date: str | None = None
    date_is_relative: bool = False

    @property
    def is_grounded(self) -> bool:
        return bool(
            self.metrics or self.dimensions or self.entity_filter_intents or self.required_tables
        )

    @property
    def is_ready(self) -> bool:
        return self.status is PlanStatus.READY

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metrics"] = [asdict(item) for item in self.metrics]
        payload["entity_filters"] = {
            key: list(values) for key, values in self.entity_filters.items()
        }
        payload["entity_filter_intents"] = [asdict(item) for item in self.entity_filter_intents]
        payload["required_joins"] = [asdict(item) for item in self.required_joins]
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> QueryPlan:
        """Rebuild complete actual-plan evidence without filling omitted defaults.

        JSON round-tripping accepts wire arrays for tuple fields while strict
        validation rejects scalar coercion. Nested records must also carry
        exactly their declared fields; unknown keys are not silently discarded.
        """
        from pydantic import TypeAdapter

        if not isinstance(payload, dict) or set(payload) != {item.name for item in fields(cls)}:
            raise ValueError("QueryPlan evidence must contain exactly all declared fields")
        record_types = {
            "metrics": MetricIntent,
            "entity_filter_intents": EntityFilterIntent,
            "required_joins": JoinIntent,
            "metric_predicates": MetricPredicate,
        }
        for key, record_type in record_types.items():
            records = payload[key]
            expected = {item.name for item in fields(record_type)}
            if not isinstance(records, (list, tuple)) or any(
                not isinstance(record, dict) or set(record) != expected for record in records
            ):
                raise ValueError(f"QueryPlan {key} must contain complete typed records")

        def validate_wire_keys(value: Any) -> None:
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("QueryPlan JSON object keys must be strings")
                for item in value.values():
                    validate_wire_keys(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    validate_wire_keys(item)

        validate_wire_keys(payload)
        try:
            rendered = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("QueryPlan evidence must be valid finite JSON") from exc
        return TypeAdapter(cls).validate_json(rendered, strict=True)
