"""Small approved evaluation fixtures used only by isolated contract tests."""

import json
from copy import deepcopy
from hashlib import sha256
from typing import Any

from tests.catalog_fixture import TEST_CATALOG
from tests.schema_fixture import synthetic_schema
from text2sql.domain.semantic_ir import parse_question_semantics
from text2sql.evaluation.service import evaluate_case
from text2sql.knowledge.governance import content_digest, sql_digest
from text2sql.knowledge.provenance import schema_fingerprint

EVALUATION_SCHEMA = synthetic_schema({"Fact": {"columns": {"Value": {"data_type": "int"}}}})


def synthetic_review(
    schema: dict[str, dict[str, Any]] | None = None, *, content: dict | None = None
) -> dict[str, Any]:
    """Explicit fictional reviewers for isolated contract tests, not business acceptance."""
    return {
        "source": "synthetic isolated test fixture",
        "business_reviewer": "synthetic-business-reviewer",
        "data_reviewer": "synthetic-data-reviewer",
        "reviewed_at": "2026-10-02T00:00:00+00:00",
        "schema_fingerprint": schema_fingerprint(schema or EVALUATION_SCHEMA),
        "content_sha256": content_digest(content or {}),
        "notes": "Fictional evidence verifies contracts only; not production accuracy or approval.",
    }


def reviewed_evaluation_case(
    case: dict[str, Any], *, schema: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Bind fake execution evidence to the exact SQL and declared fake schema.

    The Fact fixture exercises reports/fake executors, not physical binding of
    the Stat_Collection semantic plan. Dedicated plan-binding tests cover that.
    """
    case = deepcopy(case)
    case["review"] = synthetic_review(schema, content=case)
    if not case.get("should_refuse"):
        columns = case.get("expected_result_columns", ["Total"])
        rows = [{column: 1 for column in columns}]
        result_bytes = json.dumps(
            {"columns": columns, "rows": rows}, sort_keys=True, separators=(",", ":")
        ).encode()
        case["execution"] = {
            "schema_fingerprint": case["review"]["schema_fingerprint"],
            "baseline_sha256": sql_digest(case["baseline_sql"]),
            "database_snapshot_id": "synthetic-frozen-test-database",
            "environment": "synthetic-test-only",
            "executed_at": "2026-10-02T00:00:00+00:00",
            "success": True,
            "truncated": False,
            "row_count": len(rows),
            "result_columns": columns,
            "result_digest": sha256(result_bytes).hexdigest(),
        }
    return case


def plan_diagnostics(question: str) -> dict[str, Any]:
    """A fake query's actual plan evidence, separate from expected snapshots."""
    return {"semantic_ir": parse_question_semantics(question, TEST_CATALOG).to_dict()}


def _semantic_expectation(*, grounded: bool) -> dict[str, Any]:
    """Complete, static contract evidence; not a production accuracy dataset."""
    return {
        "version": 3,
        "metrics": ["collection_count"] if grounded else [],
        "metric_predicates": [],
        "dimensions": [],
        "entity_filters": {},
        "entity_filter_intents": [],
        "date_range": {"start": None, "end": None},
        "granularity": "aggregate",
        "required_tables": ["Stat_Collection"] if grounded else [],
        "result_shape": {
            "sort_direction": None,
            "sort_metric": None,
            "limit": None,
            "distinct": False,
        },
        "time_granularity": None,
        "comparison": None,
        "ambiguities": [] if grounded else ["未明确统计指标"],
        "status": "ready" if grounded else "clarification_required",
        "date_is_relative": False,
        "reference_date": None,
    }


def evaluation_cases() -> list[dict[str, Any]]:
    return [
        reviewed_evaluation_case(case)
        for case in [
            {
                "id": "positive",
                "split": "test",
                "template_id": "test-count",
                "category": "single_table",
                "difficulty": "easy",
                "question": "统计采集总人次",
                "status": "approved",
                "should_refuse": False,
                "must_execute": True,
                "baseline_sql": "SELECT COUNT(*) AS Total FROM Fact",
                "expected_semantic_ir": _semantic_expectation(grounded=True),
                "must_include_tables": ["Fact"],
                "expected_result_columns": ["Total"],
            },
            {
                "id": "refusal",
                "split": "test",
                "template_id": "test-refusal",
                "category": "refusal",
                "difficulty": "easy",
                "question": "查询未知外太空资源",
                "status": "approved",
                "should_refuse": True,
                "expected_semantic_ir": _semantic_expectation(grounded=False),
                "must_not_contain": ["Secret"],
            },
        ]
    ]


def evaluation_split_cases(split: str) -> list[dict[str, Any]]:
    """Independent, nonempty five-split fixtures with distinct SQL shapes/questions."""
    positive, refusal = evaluation_cases()
    shapes = {
        "retrieval_train": ("SELECT SUM(Value) AS Total FROM Fact", "synthetic retrieval training"),
        "retrieval_calibration": (
            "SELECT MIN(Value) AS Total FROM Fact",
            "synthetic retrieval calibration",
        ),
        "retrieval_test": ("SELECT MAX(Value) AS Total FROM Fact", "synthetic retrieval held out"),
        "dev": ("SELECT COUNT(Value) AS Total FROM Fact", "请统计采集总人次"),
        "test": (positive["baseline_sql"], positive["question"]),
    }
    sql, question = shapes[split]
    positive.update(id=f"{split}-positive", split=split, template_id=f"{split}-family")
    positive.update(baseline_sql=sql, question=question)
    positive = reviewed_evaluation_case(positive)
    refusal.update(id=f"{split}-refusal", split=split, template_id=f"{split}-refusal-family")
    if split != "test":
        refusal["question"] = f"查询未定义外太空资源 {split}"
    return [positive, reviewed_evaluation_case(refusal)]


def evaluation_results(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline = {
        "success": True,
        "rows": [{"Total": 1}],
        "columns": ["Total"],
        "row_count": 1,
        "error": None,
    }
    positive = evaluate_case(
        cases[0],
        {
            "success": True,
            "outcome": "success",
            "sql": cases[0]["baseline_sql"],
            "result": [{"Total": 1}],
            "result_columns": ["Total"],
            "result_row_count": 1,
            "candidate_tables": ["Fact"],
            "diagnostics": plan_diagnostics(cases[0]["question"]),
        },
        TEST_CATALOG,
        baseline,
    )
    refusal = evaluate_case(
        cases[1],
        {
            "success": False,
            "outcome": "refused",
            "refusal_reason": "问题超出知识范围",
            "error_code": "OUT_OF_SCOPE",
            "error": "问题超出知识范围",
            "diagnostics": plan_diagnostics(cases[1]["question"]),
        },
        TEST_CATALOG,
    )
    return [positive | {"case_index": 1}, refusal | {"case_index": 2}]
