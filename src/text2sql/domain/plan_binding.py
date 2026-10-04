"""Verify actual plan bindings against a frozen catalog, without parsing a question."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .query_plan import QueryPlan, SemanticCatalog


def _identifier(value: str) -> str:
    return value.strip("[]").casefold()


def _table(value: str) -> str:
    parts = [_identifier(part) for part in value.split(".")]
    if len(parts) == 2 and parts[0] == "dbo":
        parts = parts[1:]
    return ".".join(parts)


def _by_id(records: tuple[dict[str, Any], ...]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        result[str(record.get("id") or "")].append(record)
    return result


def validate_plan_catalog_bindings(plan: QueryPlan, catalog: SemanticCatalog) -> list[str]:
    """Check definitions and physical targets, never inventing missing plan evidence.

    IDs alone do not prove a binding: aggregation, source column, dimensional
    columns, date fields, entity values, join endpoints and aliases must retain
    the definitions of the exact catalog used by the evaluated artifact.
    """
    errors: list[str] = []
    metrics = _by_id(catalog.metrics)
    dimensions = _by_id(catalog.dimensions)
    needed_tables: set[str] = set()
    for metric in plan.metrics:
        declared = metrics.get(metric.name, [])
        if len(declared) != 1:
            errors.append(f"metric {metric.name}: missing or ambiguous frozen definition")
            continue
        record = declared[0]
        expected = (
            str(record.get("aggregation") or "").upper(),
            _table(str(record.get("source_table") or "")),
            _identifier(str(record.get("column") or "*")),
            str(record.get("output_alias") or "Value"),
        )
        actual = (
            metric.aggregate,
            _table(metric.source_table),
            _identifier(metric.column),
            metric.output_alias,
        )
        if actual != expected:
            errors.append(
                f"metric {metric.name}: aggregate/source/column/alias differs from catalog"
            )
        needed_tables.add(_table(metric.source_table))

    selected = set(plan.dimensions)
    for mapping in (
        plan.dimension_columns,
        plan.dimension_tables,
        plan.dimension_output_aliases,
    ):
        if set(mapping) != selected:
            errors.append("dimension binding keys differ from selected dimensions")
    for dimension in plan.dimensions:
        declared = dimensions.get(dimension, [])
        if len(declared) != 1 or declared[0].get("kind") == "date":
            errors.append(f"dimension {dimension}: missing or ambiguous frozen definition")
            continue
        record = declared[0]
        expected_dimension = (
            _table(str(record.get("table") or "")),
            tuple(_identifier(str(column)) for column in record.get("columns", [])),
            tuple(map(str, record.get("output_aliases", []))),
        )
        actual_dimension = (
            _table(plan.dimension_tables.get(dimension, "")),
            tuple(_identifier(column) for column in plan.dimension_columns.get(dimension, ())),
            plan.dimension_output_aliases.get(dimension, ()),
        )
        if actual_dimension != expected_dimension:
            errors.append(f"dimension {dimension}: table/columns/aliases differ from catalog")
        needed_tables.add(actual_dimension[0])

    entities: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for record in catalog.entities:
        key = (
            str(record.get("id") or ""),
            _table(str(record.get("table") or "")),
            _identifier(str(record.get("column") or "")),
        )
        entities[key].update(map(str, (record.get("values") or {}).values()))
    for record in catalog.entity_policies:
        key = (
            str(record.get("entity") or record.get("id") or ""),
            _table(str(record.get("table") or "")),
            _identifier(str(record.get("column") or "")),
        )
        entities[key].add(str(record.get("value") or ""))
    for intent in plan.entity_filter_intents:
        key = (intent.entity, _table(intent.table), _identifier(intent.column))
        if (
            not intent.values
            or key not in entities
            or not set(intent.values).issubset(entities[key])
        ):
            errors.append(f"entity {intent.entity}: target or canonical values differ from catalog")
        needed_tables.add(key[1])
    if plan.entity_filters != {
        intent.entity: intent.values for intent in plan.entity_filter_intents
    }:
        errors.append("entity summary differs from actual bound filter intents")

    declared_joins = {
        (
            _table(str(record.get("left_table") or "")),
            _identifier(str(record.get("left_column") or "")),
            _table(str(record.get("right_table") or "")),
            _identifier(str(record.get("right_column") or "")),
            str(record.get("relationship") or ""),
        )
        for record in catalog.joins
    }
    for join in plan.required_joins:
        actual_join = (
            _table(join.left_table),
            _identifier(join.left_column),
            _table(join.right_table),
            _identifier(join.right_column),
            join.relationship,
        )
        if actual_join not in declared_joins:
            errors.append("join endpoints or cardinality differ from the frozen catalog")
        needed_tables.update((actual_join[0], actual_join[2]))

    date_bindings = {
        (_table(str(record.get("table") or "")), _identifier(str(column)))
        for record in catalog.dimensions
        if record.get("kind") == "date"
        for column in record.get("columns", [])
    }
    if plan.date_table is not None or plan.date_column is not None:
        binding = (_table(plan.date_table or ""), _identifier(plan.date_column or ""))
        if binding not in date_bindings or binding[0] not in {
            _table(metric.source_table) for metric in plan.metrics
        }:
            errors.append("date table/column differs from the metric's frozen date binding")
    elif plan.date_start is not None or plan.time_granularity is not None:
        errors.append("calendar plan has no actual bound date field")
    if {_table(table) for table in plan.required_tables} != needed_tables:
        errors.append("required tables differ from the plan's physical bindings")
    selected_metrics = {metric.name for metric in plan.metrics}
    if any(predicate.metric not in selected_metrics for predicate in plan.metric_predicates):
        errors.append("metric predicate references an unselected metric")
    if plan.sort_metric is not None and plan.sort_metric not in selected_metrics:
        errors.append("sorting references an unselected metric")
    return errors
