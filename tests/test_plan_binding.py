"""Frozen-catalog bindings and literal types are part of semantic correctness."""

from dataclasses import replace

import pytest

from tests.catalog_fixture import TEST_CATALOG
from text2sql.domain.plan_binding import validate_plan_catalog_bindings
from text2sql.domain.query_plan import SemanticCatalog
from text2sql.domain.semantic_ir import parse_question_semantics
from text2sql.domain.sql_compiler import SQLCompiler
from text2sql.domain.sql_validation import validate_tsql_ast


def bound_plan():
    return parse_question_semantics("统计2025年杭州市各机构成分血采集量", TEST_CATALOG)


def test_parser_plan_has_exact_frozen_physical_bindings():
    assert validate_plan_catalog_bindings(bound_plan(), TEST_CATALOG) == []
    refused = parse_question_semantics("查询未知资源", TEST_CATALOG)
    assert validate_plan_catalog_bindings(refused, TEST_CATALOG) == []


@pytest.mark.parametrize("field", ["aggregate", "source_table", "column", "output_alias"])
def test_a_known_metric_id_does_not_authorize_another_definition(field):
    plan = bound_plan()
    wrong = replace(plan.metrics[0], **{field: "Wrong"})
    assert validate_plan_catalog_bindings(replace(plan, metrics=(wrong,)), TEST_CATALOG)


def test_dimensions_entities_dates_and_joins_are_also_bound():
    plan = bound_plan()
    mutations = (
        replace(plan, dimension_columns={"institution": ("InstID", "City")}),
        replace(plan, dimension_tables={"institution": "Stat_Collection"}),
        replace(plan, dimension_output_aliases={"institution": ("InstID", "Wrong")}),
        replace(plan, date_column="CollectionID"),
        replace(plan, date_column=None, date_table=None),
        replace(plan, required_tables=("Stat_Collection",)),
        replace(plan, required_joins=(replace(plan.required_joins[0], right_column="OrgName"),)),
        replace(
            plan, required_joins=(replace(plan.required_joins[0], relationship="one_to_many"),)
        ),
        replace(
            plan, entity_filter_intents=(replace(plan.entity_filter_intents[0], column="Secret"),)
        ),
        replace(
            plan, entity_filter_intents=(replace(plan.entity_filter_intents[0], values=("999",)),)
        ),
    )
    for wrong in mutations:
        assert validate_plan_catalog_bindings(wrong, TEST_CATALOG)


def test_case_and_explicit_dbo_identifiers_keep_the_same_physical_binding():
    plan = bound_plan()
    metric = replace(plan.metrics[0], source_table="dbo.stat_collection", column="bcpvolume")
    assert validate_plan_catalog_bindings(replace(plan, metrics=(metric,)), TEST_CATALOG) == []


@pytest.mark.parametrize("operator", ["=", "<>", "IN", "NOT IN"])
def test_entity_literals_cannot_change_type_and_trigger_implicit_casts(operator):
    catalog = SemanticCatalog(
        metrics=(
            {
                "id": "total",
                "name": "总额",
                "source_table": "Fact",
                "column": "Amount",
                "aggregation": "SUM",
                "output_alias": "Value",
            },
        ),
        entity_policies=(
            {
                "id": "kind",
                "entity": "kind",
                "terms": ["首类"],
                "table": "Fact",
                "column": "Kind",
                "value": "001",
            },
        ),
    )
    schema = {"Fact": {"columns": {"Kind": {"data_type": "nvarchar"}, "Amount": {}}}}
    question = "统计首类总额" if operator in {"=", "IN"} else "统计非首类总额"
    plan = parse_question_semantics(question, catalog)
    good = SQLCompiler().compile(plan)
    assert good is not None
    assert validate_tsql_ast(good, schema, plan) is None
    numeric = "(001)" if "IN" in operator else "001"
    wrong = f"SELECT SUM(Amount) AS Value FROM Fact WHERE Kind {operator} {numeric}"
    assert validate_tsql_ast(wrong, schema, plan) is not None
