"""Parse bounded question intents against an external semantic catalog."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Literal

from .query_plan import (
    EntityFilterIntent,
    JoinIntent,
    MetricIntent,
    MetricPredicate,
    PlanStatus,
    QueryPlan,
    SemanticCatalog,
)
from .time_resolution import resolve_date_range

__all__ = [
    "EntityFilterIntent",
    "JoinIntent",
    "MetricIntent",
    "PlanStatus",
    "QueryPlan",
    "SemanticCatalog",
    "parse_question_semantics",
    "render_semantic_ir",
]


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _unmapped_terms(
    question: str,
    catalog: SemanticCatalog,
    *,
    number_spans: tuple[tuple[int, int], ...] = (),
) -> str:
    """Conservatively retain vocabulary not covered by the catalog or supported grammar.

    This is an abstention guard, not a relevance score. Unknown constraints must
    not disappear merely because a known metric occurs elsewhere in a question.
    """
    known: set[str] = set()
    for item in (*catalog.metrics, *catalog.dimensions):
        known.update(str(value) for value in item.get("aliases", []))
        known.add(str(item.get("name") or ""))
    for item in catalog.entity_policies:
        known.update(str(value) for value in item.get("terms", []))
    for item in catalog.entities:
        known.update(str(value) for value in (item.get("values") or {}))
    grammar = (
        "帮我",
        "请",
        "查询",
        "统计",
        "汇总",
        "计算",
        "列出",
        "各个",
        "每个",
        "分别",
        "所有",
        "总计",
        "合计",
        "是多少",
        "有多少",
        "多少",
        "以及",
        "及其",
        "期间",
        "年度",
        "上半年",
        "下半年",
        "这个月",
        "上个月",
        "本月",
        "上月",
        "今年",
        "本年",
        "去年",
        "上年",
        "今天",
        "昨天",
        "季度",
        "按日",
        "每日",
        "每天",
        "按月",
        "每月",
        "各月",
        "按季",
        "每季",
        "按年",
        "每年",
        "各年",
        "最高",
        "最多",
        "最大",
        "最低",
        "最少",
        "最小",
        "从高到低",
        "从低到高",
        "升序",
        "降序",
        "不含",
        "不包括",
        "不包含",
        "排除",
        "除去",
        "不是",
        "不要",
        "大于等于",
        "小于等于",
        "不等于",
        "大于",
        "小于",
        "等于",
        "去重",
        "不重复",
        "唯一",
        "维度",
        "按",
        "各",
        "的",
        "和",
        "与",
        "且",
        "并",
        "在",
        "从",
        "至",
        "到",
        "前",
        "个",
        "名",
        "条",
        "第",
        "年",
        "月",
        "日",
        "一",
        "二",
        "三",
        "四",
        "零",
        "非",
        "top",
    )
    characters = list(question)
    for start, end in number_spans:
        for index in range(start, end):
            if characters[index].isdigit():
                characters[index] = " "
    remainder = "".join(characters)
    for term in sorted(known.union(grammar), key=len, reverse=True):
        if term:
            remainder = re.sub(re.escape(term), " ", remainder, flags=re.I)
    return re.sub(r"[\s.,，、。;；:：?？!！'\"（）()\[\]~～<>≥≤=+\-]", "", remainder)


def _negated(question: str, alias: str) -> bool:
    return bool(
        re.search(rf"(?:不含|不包括|不包含|排除|除去|非|不是|不要)\s*{re.escape(alias)}", question)
    )


def _entity_intents(question: str, catalog: SemanticCatalog) -> tuple[EntityFilterIntent, ...]:
    values: dict[tuple[str, str, str, Literal["in", "not_in"]], list[str]] = {}
    for item in catalog.entity_policies:
        for term in map(str, item.get("terms", [])):
            if term and term in question:
                operator: Literal["in", "not_in"] = "not_in" if _negated(question, term) else "in"
                key = (
                    str(item.get("entity") or item.get("id") or "entity"),
                    str(item.get("table") or ""),
                    str(item.get("column") or ""),
                    operator,
                )
                values.setdefault(key, []).append(str(item.get("value") or ""))
    for item in catalog.entities:
        aliases = item.get("values") or {}
        if not isinstance(aliases, dict):
            continue
        for alias, canonical in aliases.items():
            if alias and str(alias) in question:
                operator = "not_in" if _negated(question, str(alias)) else "in"
                key = (
                    str(item.get("id") or "entity"),
                    str(item.get("table") or ""),
                    str(item.get("column") or ""),
                    operator,
                )
                values.setdefault(key, []).append(str(canonical))
    return tuple(
        EntityFilterIntent(entity, table, column, _dedupe(items), operator)
        for (entity, table, column, operator), items in values.items()
    )


LIMIT_PATTERN = re.compile(
    r"(?:前|top\s*)(\d+)\s*(?:个|名|条)?|(?:最高|最多|最大|最低|最少|最小)的?\s*(\d+)\s*(?:个|名|条)",
    re.I,
)
DESCENDING_TERMS = ("最高", "最多", "最大", "降序", "从高到低")
ASCENDING_TERMS = ("最低", "最少", "最小", "升序", "从低到高")


def _result_shape(question: str) -> tuple[Literal["asc", "desc"] | None, int | None, bool]:
    match = LIMIT_PATTERN.search(question)
    limit = int(next(value for value in match.groups() if value)) if match else None
    descending = any(word in question for word in DESCENDING_TERMS)
    ascending = any(word in question for word in ASCENDING_TERMS)
    direction: Literal["asc", "desc"] | None = (
        "desc" if descending else "asc" if ascending else None
    )
    return direction, limit, any(word in question for word in ("去重", "不重复", "唯一"))


def _time_semantics(
    question: str,
) -> tuple[Literal["day", "month", "quarter", "year"] | None, str | None]:
    granularity: Literal["day", "month", "quarter", "year"] | None = None
    for tokens, value in (
        (("按日", "每日", "每天"), "day"),
        (("按月", "每月", "各月"), "month"),
        (("按季", "每季", "各季度"), "quarter"),
        (("按年", "每年", "各年"), "year"),
    ):
        if any(token in question for token in tokens):
            granularity = value  # type: ignore[assignment]
            break
    comparison = (
        "year_over_year"
        if "同比" in question
        else "period_over_period"
        if "环比" in question
        else None
    )
    return granularity, comparison


def parse_question_semantics(
    question: str,
    catalog: SemanticCatalog | None = None,
    *,
    clock: Callable[[], date] = date.today,
) -> QueryPlan:
    catalog = catalog or SemanticCatalog()
    normalized = re.sub(r"\s+", " ", question).strip()
    today = clock()
    metrics: list[MetricIntent] = []
    metric_aliases: dict[str, list[str]] = {}
    for item in catalog.metrics:
        aliases = [str(value) for value in item.get("aliases", [])]
        aliases.append(str(item.get("name") or ""))
        if any(alias and alias in normalized for alias in aliases):
            metric = MetricIntent(
                str(item.get("id") or "metric"),
                str(item.get("aggregation") or "").upper(),
                str(item.get("column") or "*"),
                str(item.get("output_alias") or "Value"),
                str(item.get("source_table") or ""),
            )
            metrics.append(metric)
            metric_aliases[metric.name] = aliases

    direction, limit, distinct = _result_shape(normalized)
    time_granularity, comparison = _time_semantics(normalized)
    dimensions: list[str] = []
    dimension_columns: dict[str, tuple[str, ...]] = {}
    dimension_tables: dict[str, str] = {}
    dimension_output_aliases: dict[str, tuple[str, ...]] = {}
    date_dimensions = [item for item in catalog.dimensions if item.get("kind") == "date"]
    for item in catalog.dimensions:
        if item.get("kind") == "date":
            continue
        dimension_id = str(item.get("id") or "")
        aliases = [str(value) for value in item.get("aliases", [])] + [str(item.get("name") or "")]
        selected = any(
            re.search(rf"(?:每个|各|各个|按).*?{re.escape(alias)}", normalized)
            or f"{alias}维度" in normalized
            or (limit is not None and alias in normalized)
            for alias in aliases
            if alias
        )
        if selected:
            dimensions.append(dimension_id)
            dimension_columns[dimension_id] = tuple(map(str, item.get("columns", [])))
            dimension_tables[dimension_id] = str(item.get("table") or "")
            dimension_output_aliases[dimension_id] = tuple(map(str, item.get("output_aliases", [])))

    dates = resolve_date_range(normalized, today)
    date_table = date_column = None
    relevant_dates = [
        item
        for item in date_dimensions
        if str(item.get("table") or "") in {metric.source_table for metric in metrics}
    ]
    if len(relevant_dates) == 1:
        date_table = str(relevant_dates[0].get("table") or "")
        columns = relevant_dates[0].get("columns", [])
        date_column = str(columns[0]) if columns else None

    intents = _entity_intents(normalized, catalog)
    entity_filters = {intent.entity: intent.values for intent in intents}
    required_tables = _dedupe(
        [metric.source_table for metric in metrics]
        + [intent.table for intent in intents]
        + list(dimension_tables.values())
    )
    joins = tuple(
        JoinIntent(
            str(item.get("left_table") or ""),
            str(item.get("left_column") or ""),
            str(item.get("right_table") or ""),
            str(item.get("right_column") or ""),
            str(item.get("relationship") or ""),
        )
        for item in catalog.joins
        if str(item.get("left_table") or "") in required_tables
        and str(item.get("right_table") or "") in required_tables
    )

    ambiguities: list[str] = []
    unsupported: list[str] = []
    if not metrics:
        ambiguities.append("未明确统计指标")
    if dates.issue:
        ambiguities.append(dates.issue)
    if metrics and (dates.start or time_granularity) and not date_column:
        ambiguities.append("指标需要明确唯一的业务日期字段")
    if limit is not None and (limit < 1 or direction is None):
        ambiguities.append("前 N 项需要正整数数量和明确的排序方向")
    if any(word in normalized for word in DESCENDING_TERMS) and any(
        word in normalized for word in ASCENDING_TERMS
    ):
        ambiguities.append("排序方向冲突")
    if distinct:
        ambiguities.append(
            "去重聚合需要明确去重对象和唯一键，不能使用 SELECT DISTINCT 代替指标去重"
        )
    sort_metric = metrics[0].name if direction and len(metrics) == 1 else None
    if direction and len(metrics) > 1:
        matches = [
            metric.name
            for metric in metrics
            if any(
                re.search(
                    rf"(?:按)?{re.escape(alias)}(?:从高到低|从低到高|升序|降序|最高|最低)",
                    normalized,
                )
                for alias in metric_aliases[metric.name]
                if alias
            )
        ]
        if len(matches) == 1:
            sort_metric = matches[0]
        else:
            ambiguities.append("多个指标需要明确排序指标")
    if comparison:
        unsupported.append("同比/环比需要基期、对齐口径和缺失值策略，当前规则编译器尚不支持")
    if any(word in normalized for word in ("按周", "每周", "财年", "工作日", "节假日")):
        unsupported.append("当前规则编译器只支持自然日、月、季度和年度口径")
    if any(
        word in normalized for word in ("占比", "增长率", "累计", "移动平均", "中位数", "排名并列")
    ):
        unsupported.append("当前规则编译器不支持比例、累计、窗口或复杂派生指标")
    if len({metric.source_table for metric in metrics}) > 1:
        unsupported.append("多事实表指标需要明确预聚合和防重复关联方案")
    if any(join.relationship not in {"many_to_one", "one_to_one"} for join in joins):
        unsupported.append("关联基数未证明安全，不能直接进行事实表聚合")

    metric_predicates: list[MetricPredicate] = []
    thresholds = list(
        re.finditer(
            r"(大于等于|小于等于|不等于|大于|小于|等于|>=|<=|!=|<>|≥|≤|>|<|=)"
            r"\s*(零|-?\d+(?:\.\d+)?)(?![\d.,])",
            normalized,
        )
    )
    comparison_mentions = re.finditer(
        r"大于等于|小于等于|不等于|大于|小于|等于|>=|<=|!=|<>|≥|≤|>|<|=", normalized
    )
    if any(
        not any(threshold.start() <= mention.start() < threshold.end() for threshold in thresholds)
        for mention in comparison_mentions
    ):
        ambiguities.append("存在未完整解析的阈值，所有比较条件必须明确指标和数值")
    if thresholds and len(metrics) == 1:
        operators: dict[str, Literal["gt", "gte", "lt", "lte", "eq", "neq"]] = {
            "大于": "gt",
            "大于等于": "gte",
            "小于": "lt",
            "小于等于": "lte",
            "等于": "eq",
            "不等于": "neq",
            ">=": "gte",
            "<=": "lte",
            "!=": "neq",
            "<>": "neq",
            "≥": "gte",
            "≤": "lte",
            ">": "gt",
            "<": "lt",
            "=": "eq",
        }
        for threshold in thresholds:
            metric_predicates.append(
                MetricPredicate(
                    metrics[0].name,
                    operators[threshold.group(1)],
                    "0" if threshold.group(2) == "零" else threshold.group(2),
                )
            )
        if len(thresholds) > 1 and any(
            "或" in normalized[left.end() : right.start()]
            for left, right in zip(thresholds, thresholds[1:], strict=False)
        ):
            unsupported.append("多个阈值的 OR 逻辑尚无明确的规则编译能力")
    elif thresholds:
        ambiguities.append("阈值过滤需要明确对应的唯一指标")
    elif re.search(r"大于|小于|等于|不少于|不超过|至少\d|最多\d|[<>≥≤=]", normalized):
        ambiguities.append("阈值条件尚未解析，请使用明确指标和数值比较")
    if re.search(r"不(?:大于|小于)", normalized):
        ambiguities.append("否定阈值需要明确使用大于等于或小于等于")
    polarities: dict[tuple[str, str], set[str]] = {}
    for intent in intents:
        polarities.setdefault((intent.table, intent.column), set()).add(intent.operator)
    if any(len(operators) > 1 for operators in polarities.values()):
        ambiguities.append("同一实体同时包含正向和否定条件，需要明确组合范围")
    if not ambiguities and not unsupported:
        limit_match = LIMIT_PATTERN.search(normalized)
        number_spans = (*dates.number_spans, *(item.span() for item in thresholds))
        if limit_match:
            number_spans = (*number_spans, limit_match.span())
        remainder = _unmapped_terms(normalized, catalog, number_spans=number_spans)
        if remainder:
            ambiguities.append(
                f"存在未映射的条件或语义：{remainder[:40]}；请确认业务口径或补充目录"
            )
    if dimensions:
        selected_dimension = next(
            item for item in catalog.dimensions if str(item.get("id") or "") == dimensions[0]
        )
        granularity = str(selected_dimension.get("granularity") or "grouped")
    elif any(len(intent.values) > 1 and intent.operator == "in" for intent in intents):
        granularity = "grouped_by_entity"
    else:
        granularity = "aggregate"
    if time_granularity:
        granularity = (
            f"one_row_per_{time_granularity}"
            if not dimensions
            else f"{granularity}_per_{time_granularity}"
        )
    status = (
        PlanStatus.UNSUPPORTED
        if unsupported
        else PlanStatus.CLARIFICATION_REQUIRED
        if ambiguities
        else PlanStatus.READY
    )
    return QueryPlan(
        original_question=question,
        normalized_question=normalized,
        metrics=tuple(metrics),
        dimensions=_dedupe(dimensions),
        entity_filters=entity_filters,
        entity_filter_intents=intents,
        date_start=dates.start,
        date_end=dates.end,
        expected_granularity=granularity,
        required_tables=required_tables,
        required_joins=joins,
        dimension_columns=dimension_columns,
        dimension_tables=dimension_tables,
        dimension_output_aliases=dimension_output_aliases,
        date_table=date_table,
        date_column=date_column,
        time_granularity=time_granularity,
        sort_direction=direction,
        sort_metric=sort_metric,
        limit=limit,
        distinct=distinct,
        comparison=comparison,
        metric_predicates=tuple(metric_predicates),
        ambiguities=tuple(ambiguities),
        status=status,
        diagnostics=tuple(unsupported or ambiguities),
        reference_date=today.isoformat(),
        date_is_relative=dates.relative,
    )


def render_semantic_ir(ir: QueryPlan) -> str:
    import json

    return "【问题语义中间表示（必须逐项落实）】\n" + json.dumps(
        ir.to_dict(), ensure_ascii=False, indent=2
    )
