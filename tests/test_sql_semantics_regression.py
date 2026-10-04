"""Mutation and scope regressions that previously passed AST existence checks."""

from dataclasses import replace

import pytest

from tests.catalog_fixture import TEST_CATALOG
from tests.test_semantic_ast import SCHEMA
from text2sql.domain.semantic_ir import parse_question_semantics
from text2sql.domain.sql_compiler import SQLCompiler
from text2sql.domain.sql_validation import SqlSafetyPolicy, validate_tsql_ast

QUESTION = "统计杭州市各机构2025年的成分血采集量"
PLAN = parse_question_semantics(QUESTION, TEST_CATALOG)
VALID = (
    "SELECT b.InstID, b.OrgName, SUM(a.BCPVolume) AS Volume "
    "FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID=b.InstID "
    "WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' "
    "AND a.BCType='1' AND b.City='杭州市' GROUP BY b.InstID,b.OrgName"
)


@pytest.mark.parametrize(
    "sql",
    [
        VALID.replace("a.BCType='1'", "(a.BCType='1' OR 1=1)"),
        VALID.replace("a.BCType='1'", "(a.BCType='1' OR a.BCType='0')"),
        VALID.replace("SUM(a.BCPVolume)", "SUM(a.BCPVolume * 2)"),
        VALID.replace("SUM(a.BCPVolume)", "SUM(a.BCPVolume) * 2"),
        VALID.replace("GROUP BY b.InstID,b.OrgName", "GROUP BY b.InstID,b.OrgName,a.CollectionID"),
        VALID.replace("GROUP BY b.InstID,b.OrgName", "GROUP BY b.OrgName"),
        VALID.replace("a.BTSID=b.InstID", "1=1"),
        VALID.replace("JOIN Pub_OrgAddress", "LEFT JOIN Pub_OrgAddress"),
        VALID.replace("a.BCDate < '2026-01-01'", "a.BCDate <= '2026-01-01'"),
        VALID.replace("AND a.BCType='1' ", "").replace(
            "SUM(a.BCPVolume) AS Volume",
            "SUM(CASE WHEN a.BCType='1' THEN a.BCPVolume ELSE 0 END) AS Volume",
        ),
        VALID + " HAVING SUM(a.BCPVolume)>999",
    ],
)
def test_business_mutations_are_rejected(sql):
    assert validate_tsql_ast(sql, SCHEMA, PLAN) is not None


def test_top_n_must_order_by_the_requested_metric_not_a_dimension():
    parsed = parse_question_semantics("查询2025年采集量最高的前5个机构", TEST_CATALOG)
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None
    wrong = sql[: sql.index(" ORDER BY ")] + " ORDER BY t1.OrgName DESC"
    assert validate_tsql_ast(wrong, SCHEMA, parsed) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x.DoesNotExist FROM (SELECT BCType FROM Stat_Collection) x",
        "WITH c AS (SELECT BCType FROM Stat_Collection) SELECT c.DoesNotExist FROM c",
        "SELECT ghost.BCPVolume FROM Stat_Collection a",
    ],
)
def test_unknown_derived_columns_and_aliases_fail_closed(sql):
    assert validate_tsql_ast(sql, SCHEMA) is not None


def test_projection_only_cte_tracks_renamed_physical_column_lineage():
    parsed = parse_question_semantics("统计2025年成分血采集量", TEST_CATALOG)
    sql = (
        "WITH c AS (SELECT BCPVolume AS v, BCDate AS d, BCType AS kind "
        "FROM Stat_Collection) SELECT SUM(c.v) AS Volume FROM c "
        "WHERE c.d >= '2025-01-01' AND c.d < '2026-01-01' AND c.kind='1'"
    )
    assert validate_tsql_ast(sql, SCHEMA, parsed) is None
    policy = SqlSafetyPolicy(denied_columns=("Stat_Collection.BCPVolume",))
    assert "禁止访问字段" in validate_tsql_ast(sql, SCHEMA, safety_policy=policy)


def test_unused_cte_cannot_supply_a_required_filter():
    parsed = parse_question_semantics("统计2025年成分血采集量", TEST_CATALOG)
    sql = (
        "WITH unused AS (SELECT BCType FROM Stat_Collection WHERE BCType='1') "
        "SELECT SUM(BCPVolume) AS Volume FROM Stat_Collection "
        "WHERE BCDate >= '2025-01-01' AND BCDate < '2026-01-01'"
    )
    assert validate_tsql_ast(sql, SCHEMA, parsed) is not None


def test_shadowed_aliases_are_resolved_per_scope_for_column_policy():
    sql = (
        "SELECT a.BCPVolume FROM Stat_Collection a WHERE EXISTS "
        "(SELECT 1 FROM Pub_OrgAddress a WHERE a.InstID > 0)"
    )
    policy = SqlSafetyPolicy(denied_columns=("Stat_Collection.BCPVolume",))
    assert "禁止访问字段" in validate_tsql_ast(sql, SCHEMA, safety_policy=policy)


def test_window_count_cannot_masquerade_as_an_aggregate_only_query():
    sql = "SELECT BCType, COUNT(*) OVER() AS total FROM Stat_Collection"
    policy = SqlSafetyPolicy(aggregation_only_tables=("Stat_Collection",))
    assert validate_tsql_ast(sql, SCHEMA, safety_policy=policy) is not None


def test_reordered_predicates_and_reversed_join_are_valid():
    sql = VALID.replace("a.BTSID=b.InstID", "b.InstID=a.BTSID").replace(
        "a.BCType='1' AND b.City='杭州市'", "b.City='杭州市' AND '1'=a.BCType"
    )
    assert validate_tsql_ast(sql, SCHEMA, PLAN) is None


def test_same_column_or_membership_is_not_confused_with_an_or_bypass():
    parsed = parse_question_semantics("统计2025年全血与成分血采集人次", TEST_CATALOG)
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    valid_or = sql.replace("IN (N'0', N'1')", "=N'0' OR t0.BCType=N'1'")
    # Preserve AND precedence: only the entity alternatives may form a disjunction.
    valid_or = valid_or.replace(
        "t0.[BCType] =N'0' OR t0.BCType=N'1'", "(t0.[BCType]=N'0' OR t0.BCType=N'1')"
    )
    assert validate_tsql_ast(valid_or, SCHEMA, parsed) is None


def test_compiler_rejects_an_unproven_join_cardinality():
    assert (
        SQLCompiler().compile(
            replace(
                PLAN,
                required_joins=tuple(
                    replace(join, relationship="many_to_many") for join in PLAN.required_joins
                ),
            )
        )
        is None
    )


@pytest.mark.parametrize("modifier", ["PERCENT", "WITH TIES"])
def test_top_n_percent_and_ties_are_not_treated_as_exact_n(modifier):
    parsed = parse_question_semantics("查询2025年采集量最高的前5个机构", TEST_CATALOG)
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    wrong = sql.replace("TOP (5)", f"TOP (5) {modifier}")
    assert validate_tsql_ast(wrong, SCHEMA, parsed) is not None


def test_aggregate_threshold_cannot_be_moved_to_where():
    parsed = parse_question_semantics("统计2025年采集量大于零的各机构", TEST_CATALOG)
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    without_having, predicate = sql.split(" HAVING ")
    where, group = without_having.split(" GROUP BY ")
    wrong = where + " AND " + predicate + " GROUP BY " + group
    assert validate_tsql_ast(wrong, SCHEMA, parsed) is not None


def test_schema_qualified_duplicate_table_names_do_not_mix_columns():
    schema = {
        "Facts": {"columns": {"Public": {}}, "schema_name": "dbo"},
        "analytics.Facts": {"columns": {"Secret": {}}, "schema_name": "analytics"},
    }
    policy = SqlSafetyPolicy(allowed_schemas=("dbo", "analytics"))
    assert validate_tsql_ast("SELECT Public FROM Facts", schema, safety_policy=policy) is None
    assert validate_tsql_ast("SELECT Secret FROM Facts", schema, safety_policy=policy) is not None
    assert (
        validate_tsql_ast("SELECT Secret FROM analytics.Facts", schema, safety_policy=policy)
        is None
    )
    denied = replace(policy, denied_columns=("analytics.Facts.Secret",))
    assert "禁止访问字段" in validate_tsql_ast(
        "SELECT Secret FROM analytics.Facts", schema, safety_policy=denied
    )


def test_compiler_and_lineage_preserve_explicit_non_dbo_schema():
    from text2sql.domain.query_plan import MetricIntent, QueryPlan

    schema = {"analytics.Facts": {"columns": {"Amount": {}}, "schema_name": "analytics"}}
    parsed = QueryPlan(
        original_question="total",
        normalized_question="total",
        metrics=(MetricIntent("total", "SUM", "Amount", "Total", "analytics.Facts"),),
        required_tables=("analytics.Facts",),
    )
    sql = SQLCompiler().compile(parsed)
    assert sql is not None
    assert "[analytics].[Facts]" in sql
    policy = SqlSafetyPolicy(allowed_schemas=("analytics",), allowed_tables=("analytics.Facts",))
    assert validate_tsql_ast(sql, schema, parsed, policy) is None


def test_duplicate_schema_location_fails_instead_of_merging_metadata():
    schema = {
        "Facts": {"columns": {"Public": {}}, "schema_name": "dbo"},
        "dbo.Facts": {"columns": {"Secret": {}}, "schema_name": "dbo"},
    }
    assert validate_tsql_ast("SELECT Public FROM Facts", schema) is not None
