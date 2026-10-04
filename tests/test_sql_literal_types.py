"""Entity literal types cannot be changed through SQL implicit-conversion tricks."""

import pytest

from text2sql.domain.query_plan import EntityFilterIntent, MetricIntent, QueryPlan
from text2sql.domain.sql_compiler import SQLCompiler
from text2sql.domain.sql_validation import validate_tsql_ast

SCHEMA = {"Fact": {"columns": {"Kind": {"data_type": "nvarchar"}, "ID": {"data_type": "int"}}}}
PLAN = QueryPlan(
    original_question="count kind 001",
    normalized_question="count kind 001",
    metrics=(MetricIntent("count", "COUNT", "*", "Total", "Fact"),),
    entity_filters={"kind": ("001",)},
    entity_filter_intents=(EntityFilterIntent("kind", "Fact", "Kind", ("001",)),),
    required_tables=("Fact",),
)


def test_compiled_string_entity_filter_is_valid():
    sql = SQLCompiler().compile(PLAN)
    assert sql is not None and "N'001'" in sql
    assert validate_tsql_ast(sql, SCHEMA, PLAN) is None


@pytest.mark.parametrize("replacement", ["001", "1", "1.0", "N'1'"])
def test_string_entity_cannot_be_replaced_with_numeric_or_alternate_value(replacement):
    sql = SQLCompiler().compile(PLAN)
    assert sql is not None
    assert validate_tsql_ast(sql.replace("N'001'", replacement), SCHEMA, PLAN) is not None
