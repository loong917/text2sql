"""Regression cases for calendar, polarity, ranking and supported-plan boundaries."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from tests.catalog_fixture import TEST_CATALOG
from tests.test_semantic_ast import SCHEMA
from text2sql.domain.query_plan import PlanStatus, QueryPlan
from text2sql.domain.semantic_contract import build_semantic_snapshot
from text2sql.domain.semantic_ir import parse_question_semantics
from text2sql.domain.sql_compiler import SQLCompiler
from text2sql.domain.sql_validation import validate_tsql_ast


def plan(question: str) -> QueryPlan:
    return parse_question_semantics(question, TEST_CATALOG, clock=lambda: date(2030, 1, 17))


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        ("统计2025年3月成分血采集量", "2025-03-01", "2025-04-01"),
        ("统计2024年至2025年成分血采集量", "2024-01-01", "2026-01-01"),
        ("统计2025年3月至5月成分血采集量", "2025-03-01", "2025-06-01"),
        ("统计2024年2月29日成分血采集量", "2024-02-29", "2024-03-01"),
        ("统计2025年第四季度成分血采集量", "2025-10-01", "2026-01-01"),
        ("统计去年成分血采集量", "2029-01-01", "2030-01-01"),
        ("统计上个月成分血采集量", "2029-12-01", "2030-01-01"),
        ("统计2025年上半年成分血采集量", "2025-01-01", "2025-07-01"),
        ("统计2025年1至3月成分血采集量", "2025-01-01", "2025-04-01"),
        ("统计2025-03成分血采集量", "2025-03-01", "2025-04-01"),
        ("统计2024-11至2025-03成分血采集量", "2024-11-01", "2025-04-01"),
        ("统计2024-02-29成分血采集量", "2024-02-29", "2024-03-01"),
    ],
)
def test_calendar_ranges_do_not_collapse_to_the_first_year(question, start, end):
    parsed = plan(question)
    assert parsed.status is PlanStatus.READY
    assert (parsed.date_start, parsed.date_end) == (start, end)
    assert parsed.reference_date == "2030-01-17"
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None


@pytest.mark.parametrize(
    "question",
    [
        "统计2025年13月成分血采集量",
        "统计2025年2月30日成分血采集量",
        "统计2025年1月至13月成分血采集量",
        "统计2025年至2024年成分血采集量",
        "统计2024年、2025年成分血采集量",
        "统计最近三个月成分血采集量",
        "查询前5个机构的成分血采集量",
        "统计2025年3月以前成分血采集量",
        "统计采集量不大于100的各机构",
        "统计不含杭州和宁波的成分血采集量",
    ],
)
def test_unresolved_slots_request_clarification_instead_of_guessing(question):
    parsed = plan(question)
    assert parsed.status is PlanStatus.CLARIFICATION_REQUIRED
    assert parsed.diagnostics
    assert SQLCompiler().compile(parsed) is None


def test_negation_remains_a_negative_predicate():
    parsed = plan("统计2025年不含成分血的采集人次")
    assert parsed.entity_filter_intents[0].operator == "not_in"
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert "<>" in sql
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None
    assert validate_tsql_ast(sql.replace("<>", "="), SCHEMA, parsed) is not None


@pytest.mark.parametrize(
    "question",
    [
        "统计2025年男性成分血采集量",
        "统计青岛2025年成分血采集量",
        "统计2025年成分血采集量翻倍",
        "按未定义分类统计2025年成分血采集量",
    ],
)
def test_unknown_constraints_do_not_disappear_from_known_metric_queries(question):
    parsed = plan(question)
    assert parsed.status is PlanStatus.CLARIFICATION_REQUIRED
    assert any("未映射" in reason for reason in parsed.diagnostics)
    assert SQLCompiler().compile(parsed) is None


def test_ranked_dimension_and_number_without_the_word_front_are_parsed():
    parsed = plan("查询2025年成分血采集量最高的5个机构")
    assert parsed.dimensions == ("institution",)
    assert parsed.limit == 5
    assert parsed.sort_metric == "collection_volume"
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None


@pytest.mark.parametrize("comparison", ["同比", "环比", "累计", "占比"])
def test_unproven_comparisons_have_an_explicit_capability_boundary(comparison):
    parsed = plan(f"统计2025年每月成分血采集量{comparison}")
    assert parsed.status is PlanStatus.UNSUPPORTED
    assert SQLCompiler().compile(parsed) is None
    assert validate_tsql_ast("SELECT SUM(BCPVolume) FROM Stat_Collection", SCHEMA, parsed)


@pytest.mark.parametrize("granularity", ["按日", "按月", "按季", "按年"])
def test_compiled_time_groups_are_required_by_the_sql_contract(granularity):
    parsed = plan(f"统计2025年{granularity}成分血采集量")
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None
    wrong = sql[: sql.index(" GROUP BY ")]
    assert validate_tsql_ast(wrong, SCHEMA, parsed) is not None


def test_having_threshold_is_compiled_and_checked():
    parsed = plan("列出2022年全血采集量大于零的各机构及其采集量")
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert " HAVING " in sql
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None
    assert validate_tsql_ast(sql.replace(" > 0", " > 100"), SCHEMA, parsed) is not None


def test_multi_fact_plan_never_compiles_without_a_preaggregation_contract():
    parsed = plan("统计2025年的成分血采集量")
    second = replace(parsed.metrics[0], name="other", source_table="OtherFacts")
    assert SQLCompiler().compile(replace(parsed, metrics=(*parsed.metrics, second))) is None


def test_two_metric_thresholds_remain_a_conjunction_and_keep_polarity():
    parsed = plan("统计采集量大于零且小于100的各机构")
    assert parsed.status is PlanStatus.READY
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert " > 0 AND " in sql and " < 100" in sql
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None
    negative = plan("统计采集量不等于零的各机构")
    sql = SQLCompiler().compile(negative)
    assert sql is not None and " <> 0" in sql
    assert validate_tsql_ast(sql, SCHEMA, negative) is None


@pytest.mark.parametrize(
    ("symbol", "operator", "sql_operator"),
    [
        (">=", "gte", ">="),
        ("<=", "lte", "<="),
        (">", "gt", ">"),
        ("<", "lt", "<"),
        ("=", "eq", "="),
        ("!=", "neq", "<>"),
        ("<>", "neq", "<>"),
        ("≥", "gte", ">="),
        ("≤", "lte", "<="),
    ],
)
def test_symbolic_thresholds_are_bound_not_stripped(symbol, operator, sql_operator):
    parsed = plan(f"统计2025年采集量{symbol}100的各机构")
    assert parsed.status is PlanStatus.READY
    assert [(item.operator, item.value) for item in parsed.metric_predicates] == [(operator, "100")]
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert f"HAVING SUM(t0.[BCPVolume]) {sql_operator} 100" in sql
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None


def test_threshold_year_sized_numbers_are_not_parsed_as_calendar_years():
    parsed = plan("统计采集量>=2025的各机构")
    assert parsed.status is PlanStatus.READY
    assert parsed.date_start is None and parsed.date_end is None
    assert parsed.metric_predicates[0].value == "2025"


def test_negative_threshold_compiles_and_validates():
    parsed = plan("统计采集量>-1的各机构")
    sql = SQLCompiler().compile(parsed)
    assert sql is not None and " > -1" in sql
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None


@pytest.mark.parametrize(
    "question",
    [
        "统计今年和去年采集量",
        "统计2025年1月和本月采集量",
        "统计今天和昨天采集人次",
        "统计2025年第5季度采集量",
        "统计2025年第一季度和第二季度采集量",
        "统计2025年上半年和下半年采集量",
        "统计2025年1月第一季度采集量",
        "统计2025年5日采集量",
        "统计2025-03和2025年4月采集量",
        "统计2024年至2025年第一季度采集量",
        "统计2025年采集量1234",
        "统计2025年采集量>=1,000的各机构",
        "统计采集量>100且<的各机构",
        "统计采集量>=2025-1的各机构",
        "统计采集量大于2024-2025的各机构",
        "统计2025年各机构最低采集量最高采集量",
    ],
)
def test_conflicting_or_unconsumed_slots_never_return_a_ready_plan(question):
    parsed = plan(question)
    assert parsed.status is PlanStatus.CLARIFICATION_REQUIRED
    assert SQLCompiler().compile(parsed) is None


def test_supported_relative_calendar_refinement_preserves_reference_date():
    parsed = plan("统计今年3月采集量")
    assert parsed.status is PlanStatus.READY
    assert (parsed.date_start, parsed.date_end) == ("2030-03-01", "2030-04-01")
    assert parsed.date_is_relative is True


def test_plain_language_sort_direction_is_not_consumed_without_an_order():
    parsed = plan("按采集量从高到低统计2025年各机构采集量")
    assert parsed.status is PlanStatus.READY
    assert parsed.sort_direction == "desc"
    sql = SQLCompiler().compile(parsed)
    assert sql is not None and "ORDER BY SUM(t0.[BCPVolume]) DESC" in sql


def test_deduplicated_count_needs_a_key_and_cannot_be_an_outer_distinct():
    parsed = plan("去重统计2025年采集人次")
    assert parsed.status is PlanStatus.CLARIFICATION_REQUIRED
    assert any("唯一键" in reason for reason in parsed.diagnostics)
    assert SQLCompiler().compile(parsed) is None
    ordinary = plan("统计2025年采集人次")
    assert SQLCompiler().compile(replace(ordinary, distinct=True)) is None


def test_snapshot_distinguishes_threshold_sort_metric_and_capability():
    first = plan("统计2025年采集量>100的各机构")
    second = plan("统计2025年采集量>200的各机构")
    assert build_semantic_snapshot(first) != build_semantic_snapshot(second)
    assert build_semantic_snapshot(first) != build_semantic_snapshot(
        replace(first, status=PlanStatus.UNSUPPORTED)
    )
    first = plan("统计2025年各机构采集量和采集人次按采集量降序")
    second = plan("统计2025年各机构采集量和采集人次按采集人次降序")
    assert first.status is second.status is PlanStatus.READY
    assert build_semantic_snapshot(first) != build_semantic_snapshot(second)


def test_snapshot_absolute_dates_are_stable_and_relative_dates_are_attested():
    absolute = plan("统计2025年采集量")
    later = parse_question_semantics(
        absolute.original_question, TEST_CATALOG, clock=lambda: date(2031, 2, 1)
    )
    assert build_semantic_snapshot(absolute) == build_semantic_snapshot(later)
    assert build_semantic_snapshot(absolute)["reference_date"] is None
    relative = build_semantic_snapshot(plan("统计今年采集量"))
    assert relative["date_is_relative"] is True
    assert relative["reference_date"] == "2030-01-17"


def test_actual_query_plan_roundtrips_through_json_without_defaults_or_coercion():
    original = plan("统计2025年各机构成分血采集量>=100按采集量降序")
    wire = json.loads(json.dumps(original.to_dict()))
    restored = QueryPlan.from_dict(wire)
    assert restored == original
    assert restored.status is PlanStatus.READY
    assert restored.metric_predicates[0].operator == "gte"
    assert json.loads(json.dumps(restored.to_dict())) == wire


def test_actual_query_plan_rejects_partial_or_weakly_typed_evidence():
    wire = json.loads(json.dumps(plan("统计2025年采集人次").to_dict()))
    mutations = []
    missing = deepcopy(wire)
    del missing["distinct"]
    mutations.append(missing)
    nested_missing = deepcopy(wire)
    del nested_missing["metrics"][0]["column"]
    mutations.append(nested_missing)
    nested_extra = deepcopy(wire)
    nested_extra["metrics"][0]["unknown"] = True
    mutations.append(nested_extra)
    mutations.extend([wire | {"distinct": "false"}, wire | {"limit": True}, wire | {"unknown": 1}])
    for invalid in mutations:
        with pytest.raises(ValueError):
            QueryPlan.from_dict(invalid)
