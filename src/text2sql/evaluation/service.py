"""Evaluate generation, semantic refusal and complete typed SQL results independently."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..application.ports import SqlExecutor
from ..application.text2sql_service import Text2SQLService
from ..domain.plan_binding import validate_plan_catalog_bindings
from ..domain.query_plan import QueryPlan
from ..domain.semantic_contract import build_semantic_snapshot, validate_semantic_snapshot
from ..domain.semantic_ir import SemanticCatalog
from ..domain.sql_validation import SqlSafetyPolicy, limit_tsql_rows, validate_tsql_ast
from ..knowledge.provenance import schema_fingerprint
from .comparison import ResultComparison, compare_rows, result_columns, result_rows
from .dataset import load_evaluation_cases, load_evaluation_cases_bytes
from .sql_assertions import SqlAssertions

OUTCOMES = {
    "success",
    "refused",
    "clarification_required",
    "infrastructure_error",
    "validation_failed",
    "generation_failed",
}


@dataclass(frozen=True)
class EvaluationConfig:
    dev_set_path: str
    test_set_path: str
    query_timeout_seconds: float = 30.0
    max_result_rows: int = 500
    live_schema: dict[str, dict[str, Any]] | None = None
    safety_policy: SqlSafetyPolicy | None = None

    def __post_init__(self) -> None:
        if type(self.max_result_rows) is not int or self.max_result_rows < 1:
            raise ValueError("evaluation max_result_rows must be a positive integer")

    def dataset_path(self, split: str) -> Path:
        if split not in {"dev", "test"}:
            raise ValueError("evaluation split must be 'dev' or 'test'")
        return Path(self.test_set_path if split == "test" else self.dev_set_path)


def response_outcome(response: dict[str, Any]) -> str:
    """Infrastructure failures never become semantic refusals by implication."""
    explicit = response.get("outcome")
    if isinstance(explicit, str) and explicit in OUTCOMES:
        return str(explicit)
    return "infrastructure_error"


def expected_outcome(case: dict[str, Any]) -> str:
    return str(
        case.get("expected_outcome") or ("refused" if case.get("should_refuse") else "success")
    )


def evaluate_case(
    case: dict[str, Any],
    response: dict[str, Any],
    catalog: SemanticCatalog,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce auditable per-case checks without treating execution as semantic accuracy."""
    sql = str(response.get("sql") or "")
    should_refuse = case.get("should_refuse") is True
    actual_outcome = response_outcome(response)
    refusal_reason = str(response.get("refusal_reason") or "").strip() or None
    actual_columns = [str(item) for item in response.get("result_columns", []) or []]
    actual_count = int(response.get("result_row_count", 0) or 0)
    candidate_tables = {str(item).lower() for item in response.get("candidate_tables", []) or []}
    execution_requested = not should_refuse and case.get("must_execute", True) is True
    executed = (
        response.get("success") is True
        and response.get("result") is not None
        and execution_requested
    )
    truncated = response.get("result_truncated") is True
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, expected: Any, actual: Any) -> None:
        checks.append(
            {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}
        )

    target_outcome = expected_outcome(case)
    outcome_valid = actual_outcome == target_outcome
    if should_refuse:
        outcome_valid = outcome_valid and response.get("success") is False and bool(refusal_reason)
    else:
        outcome_valid = outcome_valid and response.get("success") is True
    check("outcome", outcome_valid, target_outcome, actual_outcome)
    if should_refuse:
        check(
            "refusal_evidence",
            bool(refusal_reason) and not sql and not executed,
            True,
            bool(refusal_reason) and not sql and not executed,
        )
    else:
        expected_tables = {str(item).lower() for item in case.get("must_include_tables", []) or []}
        check(
            "retrieval_recall",
            expected_tables.issubset(candidate_tables),
            sorted(expected_tables),
            sorted(candidate_tables),
        )
        if execution_requested:
            check("execution_success", executed, True, executed)

    expected_ir = case.get("expected_semantic_ir")
    if isinstance(expected_ir, dict):
        snapshot: dict[str, Any] | None = None
        evidence_error = "missing actual QueryPlan evidence"
        try:
            diagnostics = response.get("diagnostics")
            if not isinstance(diagnostics, dict):
                raise ValueError(evidence_error)
            plan = QueryPlan.from_dict(diagnostics.get("semantic_ir"))
            if plan.original_question != str(case.get("question") or "").strip():
                raise ValueError("actual QueryPlan evidence belongs to a different question")
            actual = build_semantic_snapshot(plan)
            errors = validate_semantic_snapshot(actual)
            errors.extend(validate_plan_catalog_bindings(plan, catalog))
            if errors:
                raise ValueError("; ".join(errors))
            snapshot = actual
        except ValueError as exc:
            evidence_error = str(exc)
        for key, expected in expected_ir.items():
            actual_value = (
                snapshot.get(key) if snapshot is not None else {"evidence_error": evidence_error}
            )
            check(
                f"semantic_ir:{key}",
                snapshot is not None and actual_value == expected,
                expected,
                actual_value,
            )

    assertions = SqlAssertions(sql)
    if not should_refuse:
        check("sql_generated", bool(sql), "non-empty sql", bool(sql))
        check(
            "sql_parse",
            assertions.tree is not None,
            "single T-SQL query",
            assertions.tree is not None,
        )
    checks.extend(assertions.checks(case))
    for column in case.get("expected_result_columns", []) or []:
        check(f"result_column:{column}", column in actual_columns, column, actual_columns)
    for field, predicate in (
        ("min_result_rows", lambda value: actual_count >= value),
        ("max_result_rows", lambda value: actual_count <= value),
    ):
        if field in case:
            check(field, predicate(case[field]), case[field], actual_count)

    comparison = ResultComparison.from_case(case)
    baseline_success = baseline is not None and baseline.get("success") is True
    baseline_complete = False
    if execution_requested:
        check("baseline_execution_success", baseline_success, True, baseline_success)
        baseline_columns = list(baseline.get("columns") or []) if baseline else []
        try:
            actual_rows = result_rows(response.get("result"))
            expected_rows = (
                result_rows(baseline.get("rows")) if baseline_success and baseline else []
            )
            baseline_complete = (
                executed
                and baseline_success
                and not truncated
                and not (baseline and baseline.get("truncated"))
                and actual_count == len(actual_rows)
                and baseline is not None
                and baseline.get("row_count") == len(expected_rows)
            )
        except ValueError:
            actual_rows, expected_rows = [], []
        check("baseline_result_complete", baseline_complete, True, baseline_complete)
        check(
            "baseline_result_columns",
            baseline_complete and actual_columns == baseline_columns,
            baseline_columns,
            actual_columns,
        )
        baseline_count = int(baseline.get("row_count") or 0) if baseline else 0
        check(
            "baseline_result_row_count",
            baseline_complete and actual_count == baseline_count,
            baseline_count,
            actual_count,
        )
        matches = (
            baseline_complete
            and actual_columns == baseline_columns
            and compare_rows(actual_rows, expected_rows, actual_columns, comparison)
        )
        check(
            "baseline_result_match",
            matches,
            comparison.to_dict(),
            {"matched": matches, "complete": baseline_complete},
        )

    return {
        "id": str(case.get("id") or ""),
        "split": str(case.get("split") or ""),
        "category": str(case.get("category") or ""),
        "difficulty": str(case.get("difficulty") or ""),
        "question": str(case.get("question") or ""),
        "passed": bool(checks) and all(item["passed"] for item in checks),
        "should_refuse": should_refuse,
        "outcome": actual_outcome,
        "refusal_reason": refusal_reason,
        "error_code": str(response.get("error_code") or "") or None,
        "actual_sql": sql,
        "executed": executed,
        "result_columns": actual_columns,
        "candidate_tables": sorted(candidate_tables),
        "result_row_count": actual_count,
        "result_truncated": truncated,
        "baseline_compared": baseline is not None,
        "baseline_complete": baseline_complete,
        "baseline_succeeded": baseline_success,
        "baseline_result_columns": list(baseline.get("columns") or []) if baseline else [],
        "baseline_result_row_count": int(baseline.get("row_count") or 0) if baseline else 0,
        "baseline_error": str(baseline.get("error") or "") or None if baseline else None,
        "error": str(response.get("error") or "") or None,
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

    async def aclose(self) -> None:
        try:
            close_query = getattr(self._query_service, "aclose", None)
            if close_query is not None:
                await close_query()
        finally:
            close_executor = getattr(self._sql_executor, "aclose", None)
            if close_executor is not None:
                await close_executor()

    async def run(
        self, split: str = "dev", *, dataset_bytes: bytes | None = None
    ) -> list[dict[str, Any]]:
        path = self._config.dataset_path(split)
        cases = (
            load_evaluation_cases_bytes(dataset_bytes, source=path, expected_split=split)
            if dataset_bytes is not None
            else load_evaluation_cases(path, expected_split=split)
        )
        if not cases:
            raise ValueError(f"evaluation split '{split}' is empty")
        if self._config.live_schema:
            digest = schema_fingerprint(self._config.live_schema)
            if any(case.payload["review"]["schema_fingerprint"] != digest for case in cases):
                raise ValueError("evaluation review evidence belongs to a different Schema")
        results: list[dict[str, Any]] = []
        for index, loaded in enumerate(cases, 1):
            case = loaded.payload
            try:
                response = await self._query_service.generate(
                    question=loaded.question,
                    max_retries=1,
                    execute_sql=not case.get("should_refuse") and case.get("must_execute", True),
                    capture_feedback=False,
                    preserve_result_types=True,
                )
            except Exception as exc:
                response = {"success": False, "outcome": "infrastructure_error", "error": str(exc)}
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
            or response.get("success") is not True
        ):
            return None
        try:
            # Dataset approval is not SQL execution authority. Baselines use
            # the independently frozen schema and the same access policy as
            # application queries, never a plan inferred from model output.
            if not self._config.live_schema or self._config.safety_policy is None:
                raise ValueError(
                    "baseline execution requires an authorized schema and safety policy"
                )
            validation_error = validate_tsql_ast(
                sql,
                self._config.live_schema,
                safety_policy=self._config.safety_policy,
            )
            if validation_error:
                raise ValueError(f"baseline SQL admission failed: {validation_error}")
            failed_assertions = [
                item["name"] for item in SqlAssertions(sql).checks(case) if not item["passed"]
            ]
            if failed_assertions:
                raise ValueError(
                    "baseline SQL conflicts with declared case constraints: "
                    + ", ".join(failed_assertions)
                )
            value = await self._sql_executor.execute(
                limit_tsql_rows(sql, self._config.max_result_rows + 1),
                timeout_seconds=self._config.query_timeout_seconds,
            )
            observed_rows = result_rows(value)
            rows = observed_rows[: self._config.max_result_rows]
            return {
                "success": True,
                "rows": rows,
                "columns": result_columns(value),
                "row_count": len(rows),
                "truncated": len(observed_rows) > self._config.max_result_rows,
                "error": None,
            }
        except Exception as exc:
            return {
                "success": False,
                "rows": [],
                "columns": [],
                "row_count": 0,
                "truncated": False,
                "error": str(exc),
            }
