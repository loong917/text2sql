"""Strict business contracts and authored truth stay separate from predictions."""

import json

import pytest

from tests.evaluation_fixture import synthetic_review
from tests.test_structured_knowledge import GOLD_TRUTH, SCHEMA, reviewed_gold
from text2sql.knowledge.models import MetricRecord, PolicyRecord
from text2sql.knowledge.structured import (
    KnowledgeBundle,
    load_knowledge_bundle,
    load_validated_knowledge_bundle,
    validate_bundle_schema,
)


@pytest.mark.parametrize("aggregation,column", [("CUSTOM", "Amount"), ("SUM", "*"), ("AVG", "*")])
def test_unknown_or_invalid_aggregation_is_rejected(aggregation, column):
    with pytest.raises(ValueError):
        MetricRecord.model_validate(
            {
                "id": "m",
                "name": "m",
                "source_table": "Fact",
                "aggregation": aggregation,
                "column": column,
            }
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"id": "unknown", "kind": "unknown", "anything": "accepted before"},
        {"id": "incomplete", "kind": "entity_filter", "table": "Fact"},
        {"id": "join", "kind": "required_join", "terms": ["join"]},
        {"id": "refusal", "kind": "refusal", "rule": "reject", "ignored_extra": True},
    ],
)
def test_policy_requires_an_implemented_complete_contract(policy):
    with pytest.raises(ValueError):
        PolicyRecord.model_validate(policy)


def test_declared_cardinality_requires_unique_key_proof():
    schema = json.loads(json.dumps(SCHEMA))
    schema["Dim"]["unique_keys"] = {}
    bundle = KnowledgeBundle(
        joins=[
            {
                "id": "j",
                "left_table": "Fact",
                "left_column": "DimID",
                "right_table": "Dim",
                "right_column": "ID",
                "relationship": "many_to_one",
            }
        ]
    )
    validate_bundle_schema(bundle, schema)
    assert bundle.joins == []
    assert any("cardinality" in error for error in bundle.errors)


def test_review_cannot_survive_a_unit_or_alias_change():
    metric = {
        "id": "m",
        "name": "sum",
        "source_table": "Fact",
        "aggregation": "SUM",
        "column": "Amount",
        "unit": "test-unit",
    }
    metric["review"] = synthetic_review(SCHEMA, content=metric)
    MetricRecord.model_validate(metric)
    with pytest.raises(ValueError, match="current authored content"):
        MetricRecord.model_validate(metric | {"unit": "changed-test-unit"})


def test_case_insensitive_join_reference_does_not_bypass_key_proof_or_crash():
    join = {
        "id": "j",
        "left_table": "fact",
        "left_column": "dimid",
        "right_table": "dim",
        "right_column": "id",
        "relationship": "many_to_one",
    }
    bundle = KnowledgeBundle(joins=[join])
    validate_bundle_schema(bundle, SCHEMA)
    assert bundle.joins == [join]
    assert not bundle.errors


def test_authored_gold_semantics_are_not_ignored(tmp_path):
    (tmp_path / "domain").mkdir()
    (tmp_path / "examples").mkdir()
    (tmp_path / "manifest.json").write_text('{"schema_version":1}', encoding="utf-8")
    (tmp_path / "domain/metrics.json").write_text(
        json.dumps(
            [
                {
                    "id": "amount_sum",
                    "name": "sum",
                    "source_table": "Fact",
                    "aggregation": "SUM",
                    "column": "Amount",
                }
            ]
        ),
        encoding="utf-8",
    )
    for bad_truth in (GOLD_TRUTH | {"metrics": ["wrong_metric"]}, GOLD_TRUTH | {"version": 2}):
        (tmp_path / "examples/gold_sql.jsonl").write_text(
            json.dumps(reviewed_gold() | {"semantic_ir": bad_truth}) + "\n", encoding="utf-8"
        )
        bundle = load_knowledge_bundle(tmp_path, SCHEMA)
        assert not bundle.gold_sql
        assert bundle.errors


def test_production_knowledge_cannot_use_unreviewed_business_units(tmp_path):
    (tmp_path / "domain").mkdir()
    (tmp_path / "manifest.json").write_text('{"schema_version":1}', encoding="utf-8")
    (tmp_path / "domain/metrics.json").write_text(
        json.dumps(
            [
                {
                    "id": "amount_sum",
                    "name": "sum",
                    "source_table": "Fact",
                    "aggregation": "SUM",
                    "column": "Amount",
                    "unit": "数据库原始单位",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="review required"):
        load_validated_knowledge_bundle(tmp_path, SCHEMA, require_reviewed=True)
