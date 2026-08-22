"""Deterministic question semantics used to ground and validate Text2SQL."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


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


@dataclass(frozen=True)
class JoinIntent:
    left_table: str
    left_column: str
    right_table: str
    right_column: str


@dataclass(frozen=True)
class SemanticCatalog:
    metrics: tuple[dict[str, Any], ...] = ()
    dimensions: tuple[dict[str, Any], ...] = ()
    entity_policies: tuple[dict[str, Any], ...] = ()
    joins: tuple[dict[str, Any], ...] = ()
    entities: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class QuestionSemanticIR:
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
    date_table: str | None = None
    date_column: str | None = None
    time_granularity: str | None = None
    sort_direction: str | None = None
    limit: int | None = None
    distinct: bool = False
    comparison: str | None = None
    ambiguities: tuple[str, ...] = ()

    @property
    def is_grounded(self) -> bool:
        """Whether the question maps to at least one catalog-backed concept."""
        return bool(
            self.metrics or self.dimensions or self.entity_filter_intents or self.required_tables
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metrics"] = [asdict(item) for item in self.metrics]
        payload["entity_filters"] = {
            key: list(values) for key, values in self.entity_filters.items()
        }
        payload["entity_filter_intents"] = [asdict(item) for item in self.entity_filter_intents]
        payload["required_joins"] = [asdict(item) for item in self.required_joins]
        return payload


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _catalog_entity_intents(
    question: str, catalog: SemanticCatalog
) -> tuple[EntityFilterIntent, ...]:
    intents: list[EntityFilterIntent] = []
    for item in catalog.entities:
        values = item.get("values") or {}
        if not isinstance(values, dict):
            continue
        selected = _dedupe(
            [str(canonical) for alias, canonical in values.items() if str(alias) in question]
        )
        if selected:
            intents.append(
                EntityFilterIntent(
                    str(item.get("id") or "entity"),
                    str(item.get("table") or ""),
                    str(item.get("column") or ""),
                    selected,
                )
            )
    return tuple(intents)


def _extract_year_range(question: str) -> tuple[str | None, str | None]:
    years = [int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?:年|度)?", question)]
    if years:
        year = years[0]
    elif "今年" in question or "本年" in question:
        year = date.today().year
    elif "去年" in question or "上年" in question:
        year = date.today().year - 1
    else:
        return None, None
    return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"


def _extract_result_shape(question: str) -> tuple[str | None, int | None, bool]:
    limit_match = re.search(r"(?:前|top\s*)(\d+)\s*(?:个|名|条)?", question, flags=re.I)
    limit = int(limit_match.group(1)) if limit_match else None
    descending = any(token in question for token in ("最高", "最多", "最大", "降序"))
    ascending = any(token in question for token in ("最低", "最少", "最小", "升序"))
    direction = "desc" if descending else "asc" if ascending else None
    return direction, limit, any(token in question for token in ("去重", "不重复", "唯一"))


def _extract_time_semantics(question: str) -> tuple[str | None, str | None]:
    granularity = next(
        (
            value
            for tokens, value in (
                (("按日", "每日", "每天"), "day"),
                (("按月", "每月", "各月"), "month"),
                (("按季", "季度", "各季度"), "quarter"),
                (("按年", "每年", "年度"), "year"),
            )
            if any(token in question for token in tokens)
        ),
        None,
    )
    comparison = (
        "year_over_year"
        if "同比" in question
        else "period_over_period"
        if "环比" in question
        else None
    )
    return granularity, comparison


def parse_question_semantics(
    question: str, catalog: SemanticCatalog | None = None
) -> QuestionSemanticIR:
    catalog = catalog or SemanticCatalog()
    normalized = re.sub(r"\s+", " ", question).strip()
    metrics: list[MetricIntent] = []
    for item in catalog.metrics:
        aliases = [str(value) for value in item.get("aliases", [])]
        if str(item.get("name") or ""):
            aliases.append(str(item["name"]))
        if any(alias and alias in normalized for alias in aliases):
            metrics.append(
                MetricIntent(
                    str(item.get("id") or "metric"),
                    str(item.get("aggregation") or "").upper(),
                    str(item.get("column") or "*"),
                    str(item.get("output_alias") or "Value"),
                    str(item.get("source_table") or ""),
                )
            )

    dimensions: list[str] = []
    dimension_columns: dict[str, tuple[str, ...]] = {}
    date_table = date_column = None
    for item in catalog.dimensions:
        dimension_id = str(item.get("id") or "")
        aliases = [str(value) for value in item.get("aliases", [])]
        aliases.append(str(item.get("name") or ""))
        selected = any(
            re.search(rf"(?:每个|各|各个|按).*?{re.escape(alias)}", normalized)
            or f"{alias}维度" in normalized
            for alias in aliases
            if alias
        )
        if selected and item.get("kind") != "date":
            dimensions.append(dimension_id)
            dimension_columns[dimension_id] = tuple(str(v) for v in item.get("columns", []))
        if item.get("kind") == "date" or dimension_id.endswith("date"):
            columns = item.get("columns", [])
            if columns:
                date_table = str(item.get("table") or "")
                date_column = str(columns[0])

    entity_filters: dict[str, tuple[str, ...]] = {}
    intent_values: dict[tuple[str, str, str], list[str]] = {}
    for item in catalog.entity_policies:
        if any(str(term) in normalized for term in item.get("terms", [])):
            entity = str(item.get("entity") or item.get("id") or "entity")
            key = (entity, str(item.get("table") or ""), str(item.get("column") or ""))
            intent_values.setdefault(key, []).append(str(item.get("value") or ""))
    policy_intents = tuple(
        EntityFilterIntent(entity, table, column, _dedupe(values))
        for (entity, table, column), values in intent_values.items()
    )
    entity_filter_intents = policy_intents + _catalog_entity_intents(normalized, catalog)
    for intent in entity_filter_intents:
        entity_filters[intent.entity] = intent.values

    date_start, date_end = _extract_year_range(normalized)
    sort_direction, limit, distinct = _extract_result_shape(normalized)
    time_granularity, comparison = _extract_time_semantics(normalized)
    required_tables = [item.source_table for item in metrics if item.source_table]
    required_tables.extend(intent.table for intent in entity_filter_intents if intent.table)
    for item in catalog.dimensions:
        if str(item.get("id") or "") in dimensions:
            required_tables.append(str(item.get("table") or ""))
    ambiguities: list[str] = []
    if not metrics and any(token in normalized for token in ("统计", "查询", "多少")):
        ambiguities.append("未明确统计指标")

    selected_dimension = next(
        (item for item in catalog.dimensions if str(item.get("id") or "") in dimensions), None
    )
    if selected_dimension:
        granularity = str(selected_dimension.get("granularity") or "grouped")
    elif any(len(intent.values) > 1 for intent in entity_filter_intents):
        granularity = "grouped_by_entity"
    else:
        granularity = "aggregate"

    return QuestionSemanticIR(
        original_question=question,
        normalized_question=normalized,
        metrics=tuple(metrics),
        dimensions=_dedupe(dimensions),
        entity_filters=entity_filters,
        entity_filter_intents=entity_filter_intents,
        date_start=date_start,
        date_end=date_end,
        expected_granularity=granularity,
        required_tables=_dedupe(required_tables),
        required_joins=tuple(
            JoinIntent(
                str(item.get("left_table") or ""),
                str(item.get("left_column") or ""),
                str(item.get("right_table") or ""),
                str(item.get("right_column") or ""),
            )
            for item in catalog.joins
            if str(item.get("left_table") or "") in required_tables
            and str(item.get("right_table") or "") in required_tables
        ),
        dimension_columns=dimension_columns,
        date_table=date_table,
        date_column=date_column,
        time_granularity=time_granularity,
        sort_direction=sort_direction,
        limit=limit,
        distinct=distinct,
        comparison=comparison,
        ambiguities=tuple(ambiguities),
    )


def render_semantic_ir(ir: QuestionSemanticIR) -> str:
    import json

    return "【问题语义中间表示（必须逐项落实）】\n" + json.dumps(
        ir.to_dict(), ensure_ascii=False, indent=2
    )
