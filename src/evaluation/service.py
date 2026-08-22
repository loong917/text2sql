"""Evaluation orchestration kept separate from offline knowledge training."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..application.ports import SqlExecutor
from ..application.text2sql_service import Text2SQLService
from ..domain.semantic_ir import SemanticCatalog, parse_question_semantics
from .dataset import load_evaluation_cases
from .semantic_snapshot import build_semantic_snapshot


@dataclass(frozen=True)
class EvaluationConfig:
    """Paths and runtime limits required by the isolated evaluator."""

    dev_set_path: str
    test_set_path: str
    query_timeout_seconds: float = 30.0

    def dataset_path(self, split: str) -> Path:
        if split not in {"dev", "test"}:
            raise ValueError("evaluation split must be 'dev' or 'test'")
        return Path(self.test_set_path if split == "test" else self.dev_set_path)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("[", "").replace("]", "")).strip()


def _contains(sql: str, expected: str) -> bool:
    normalized_sql = _normalize_text(sql)
    normalized_expected = _normalize_text(expected)
    if not normalized_expected:
        return True
    join = re.match(r"^(?:[\w\[\]\.]+)\.([\w\[\]]+)\s*=\s*(?:[\w\[\]\.]+)\.([\w\[\]]+)$", expected)
    if join:
        actual_pairs = {
            tuple(sorted((left.split(".")[-1].strip("[]"), right.split(".")[-1].strip("[]"))))
            for left, right in re.findall(
                r"(?i)\b([\w\[\]\.]+)\s*=\s*([\w\[\]\.]+)\b", normalized_sql
            )
            if "." in left and "." in right
        }
        if tuple(sorted((join.group(1).strip("[]"), join.group(2).strip("[]")))) in actual_pairs:
            return True
    return normalized_expected in normalized_sql


def _rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict(orient="records")
    return (
        [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    )


def _normalized_rows(value: Any) -> list[dict[str, str]]:
    rows = [
        {str(key): "" if item is None else str(item) for key, item in row.items()}
        for row in _rows(value)
    ]
    return sorted(rows, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))


def _semantic_projection(question: str, catalog: SemanticCatalog) -> dict[str, Any]:
    return build_semantic_snapshot(parse_question_semantics(question, catalog))


def evaluate_case(
    case: dict[str, Any],
    response: dict[str, Any],
    catalog: SemanticCatalog,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one response using declarative assertions from the dataset."""
    sql = str(response.get("sql") or "")
    should_refuse = bool(case.get("should_refuse"))
    actual_columns = [str(item) for item in response.get("result_columns", []) or []]
    actual_count = int(response.get("result_row_count", 0) or 0)
    candidate_tables = {str(item).lower() for item in response.get("candidate_tables", []) or []}
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, expected: Any, actual: Any) -> None:
        checks.append({"name": name, "passed": passed, "expected": expected, "actual": actual})

    check(
        "should_refuse" if should_refuse else "should_succeed",
        not response.get("success") if should_refuse else bool(response.get("success")),
        True,
        bool(response.get("success")),
    )
    if not should_refuse:
        expected_tables = {str(item).lower() for item in case.get("must_include_tables", []) or []}
        check(
            "retrieval_recall",
            expected_tables.issubset(candidate_tables),
            sorted(expected_tables),
            sorted(candidate_tables),
        )
        if bool(case.get("must_execute", True)):
            check(
                "execution_success",
                bool(response.get("success")) and response.get("result") is not None,
                True,
                bool(response.get("success")) and response.get("result") is not None,
            )
    expected_ir = case.get("expected_semantic_ir")
    if isinstance(expected_ir, dict):
        actual_ir = _semantic_projection(str(case.get("question") or ""), catalog)
        for key, expected in expected_ir.items():
            check(
                f"semantic_ir:{key}", actual_ir.get(key) == expected, expected, actual_ir.get(key)
            )
    assertions = {
        "must_include_tables": "table",
        "must_include_columns": "column",
        "must_include_filters": "filter",
        "must_include_joins": "join",
    }
    for field, label in assertions.items():
        for expected in map(str, case.get(field, []) or []):
            check(f"{label}:{expected}", _contains(sql, expected), expected, sql)
    for field, label in (
        ("must_not_contain", "not_contains"),
        ("must_not_include_columns", "not_column"),
    ):
        for forbidden in map(str, case.get(field, []) or []):
            check(f"{label}:{forbidden}", not _contains(sql, forbidden), forbidden, sql)
    if "must_have_group_by" in case:
        expected = bool(case["must_have_group_by"])
        actual = "group by" in _normalize_text(sql)
        check("group_by", actual is expected, expected, actual)
    for expected in map(str, case.get("expected_result_columns", []) or []):
        check(f"result_column:{expected}", expected in actual_columns, expected, actual_columns)
    if "min_result_rows" in case:
        expected = int(case["min_result_rows"])
        check("min_result_rows", actual_count >= expected, expected, actual_count)
    if "max_result_rows" in case:
        expected = int(case["max_result_rows"])
        check("max_result_rows", actual_count <= expected, expected, actual_count)
    if baseline is not None:
        baseline_rows = _normalized_rows(baseline.get("rows"))
        actual_rows = _normalized_rows(response.get("result"))
        check(
            "baseline_execution_success",
            bool(baseline.get("success")),
            True,
            bool(baseline.get("success")),
        )
        check(
            "baseline_result_columns",
            actual_columns == baseline.get("columns", []),
            baseline.get("columns", []),
            actual_columns,
        )
        check(
            "baseline_result_row_count",
            actual_count == baseline.get("row_count", 0),
            baseline.get("row_count", 0),
            actual_count,
        )
        check("baseline_result_match", actual_rows == baseline_rows, baseline_rows, actual_rows)
    if not should_refuse and not sql:
        check("sql_generated", False, "non-empty sql", sql)
    return {
        "id": str(case.get("id") or ""),
        "split": str(case.get("split") or ""),
        "category": str(case.get("category") or ""),
        "difficulty": str(case.get("difficulty") or ""),
        "question": str(case.get("question") or ""),
        "passed": bool(checks) and all(item["passed"] for item in checks),
        "should_refuse": should_refuse,
        "actual_sql": sql,
        "executed": bool(not should_refuse and case.get("must_execute", True)),
        "result_columns": actual_columns,
        "result_row_count": actual_count,
        "baseline_compared": baseline is not None,
        "baseline_error": None if baseline is None else baseline.get("error"),
        "error": response.get("error"),
        "checks": checks,
    }


class EvaluationService:
    def __init__(
        self,
        query_service: Text2SQLService,
        sql_executor: SqlExecutor,
        catalog: SemanticCatalog,
        config: EvaluationConfig,
    ):
        self._query_service = query_service
        self._sql_executor = sql_executor
        self._catalog = catalog
        self._config = config

    async def run(self, split: str = "dev") -> list[dict[str, Any]]:
        path = self._config.dataset_path(split)
        cases = load_evaluation_cases(path, expected_split=split)
        if not cases:
            raise ValueError(f"evaluation split '{split}' is empty: {path}")
        results: list[dict[str, Any]] = []
        for index, loaded in enumerate(cases, 1):
            case = loaded.payload
            question = str(case.get("question") or "").strip()
            should_refuse = bool(case.get("should_refuse"))
            response = await self._query_service.generate(
                question=question,
                max_retries=1,
                execute_sql=not should_refuse and bool(case.get("must_execute", True)),
                capture_feedback=False,
            )
            baseline = await self._baseline(case, response)
            result = evaluate_case(case, response, self._catalog, baseline)
            result["case_index"] = index
            results.append(result)
        return results

    async def _baseline(
        self, case: dict[str, Any], response: dict[str, Any]
    ) -> dict[str, Any] | None:
        sql = str(case.get("baseline_sql") or "").strip()
        if (
            not sql
            or case.get("should_refuse")
            or not case.get("must_execute", True)
            or not response.get("success")
        ):
            return None
        try:
            value = await self._sql_executor.execute(
                sql, timeout_seconds=self._config.query_timeout_seconds
            )
            rows = _rows(value)
            return {
                "success": True,
                "rows": rows,
                "columns": list(rows[0]) if rows else [],
                "row_count": len(rows),
                "error": None,
            }
        except Exception as exc:
            return {"success": False, "rows": [], "columns": [], "row_count": 0, "error": str(exc)}
