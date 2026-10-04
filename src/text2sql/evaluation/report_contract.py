"""Strict frozen-evaluation evidence contracts and per-case integrity checks."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..domain.semantic_contract import SEMANTIC_SNAPSHOT_KEYS, validate_semantic_snapshot
from .artifact_evidence import ARTIFACT_HASH_FIELDS
from .comparison import ResultComparison
from .dataset import EvaluationCase
from .service import expected_outcome
from .sql_assertions import SqlAssertions

EVALUATION_REPORT_VERSION = 2


class EvidenceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class EvaluationCheck(EvidenceModel):
    name: str = Field(min_length=1)
    passed: bool
    expected: Any
    actual: Any


class EvaluationResult(EvidenceModel):
    id: str = Field(min_length=1)
    split: Literal["dev", "test"]
    category: str
    difficulty: str
    question: str = Field(min_length=1)
    case_index: int = Field(ge=1)
    passed: bool
    should_refuse: bool
    outcome: Literal[
        "success",
        "refused",
        "clarification_required",
        "infrastructure_error",
        "validation_failed",
        "generation_failed",
    ]
    refusal_reason: str | None
    error_code: str | None
    actual_sql: str
    executed: bool
    candidate_tables: list[str]
    result_columns: list[str]
    result_row_count: int = Field(ge=0)
    result_truncated: bool
    baseline_compared: bool
    baseline_complete: bool
    baseline_succeeded: bool
    baseline_result_columns: list[str]
    baseline_result_row_count: int = Field(ge=0)
    baseline_error: str | None
    error: str | None
    checks: list[EvaluationCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def frozen_execution_evidence(self) -> EvaluationResult:
        if len({item.name for item in self.checks}) != len(self.checks):
            raise ValueError("evaluation check names must be unique")
        if self.split == "test":
            semantic_checks = [item for item in self.checks if item.name.startswith("semantic_ir:")]
            semantic_names = {item.name for item in semantic_checks}
            if semantic_names != {f"semantic_ir:{key}" for key in SEMANTIC_SNAPSHOT_KEYS}:
                raise ValueError("frozen cases require the complete current semantic checks")
            expected_snapshot = {
                item.name.removeprefix("semantic_ir:"): item.expected
                for item in self.checks
                if item.name.startswith("semantic_ir:")
            }
            if validate_semantic_snapshot(expected_snapshot):
                raise ValueError("frozen semantic checks require the complete current protocol")
            if any(item.passed is not (item.actual == item.expected) for item in semantic_checks):
                raise ValueError("semantic check verdict disagrees with actual/expected evidence")
            if all(item.passed for item in semantic_checks):
                actual_snapshot = {
                    item.name.removeprefix("semantic_ir:"): item.actual for item in semantic_checks
                }
                if validate_semantic_snapshot(actual_snapshot):
                    raise ValueError("passed semantic evidence requires a valid actual snapshot")
        if self.split == "test" and not self.should_refuse:
            required = {
                "execution_success",
                "baseline_execution_success",
                "baseline_result_complete",
                "baseline_result_columns",
                "baseline_result_row_count",
                "baseline_result_match",
            }
            if not required.issubset({item.name for item in self.checks}):
                raise ValueError("frozen positive cases require complete execution checks")
        if self.baseline_complete and (
            not self.executed
            or not self.baseline_compared
            or not self.baseline_succeeded
            or self.result_truncated
            or self.baseline_error
        ):
            raise ValueError("complete baseline evidence requires successful untruncated execution")
        if any(
            item.name == "baseline_result_match" and item.passed and not self.baseline_complete
            for item in self.checks
        ):
            raise ValueError("baseline match requires complete result evidence")
        return self


class EvaluationAttestation(EvidenceModel):
    artifact_version: str | None
    schema_fingerprint: str | None
    dataset_sha256: str
    artifact_sha256: dict[str, str]
    release_identity: dict[str, Any]

    @field_validator("artifact_sha256")
    @classmethod
    def complete_artifact_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != ARTIFACT_HASH_FIELDS:
            raise ValueError("attestation requires the complete artifact hash set")
        if any(
            len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
            for item in value.values()
        ):
            raise ValueError("artifact hashes must be lowercase SHA-256 values")
        return value


class CheckMetric(EvidenceModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)


class EvaluationSummary(EvidenceModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    refusal_total: int = Field(ge=0)
    refusal_passed: int = Field(ge=0)
    refusal_pass_rate: float = Field(ge=0, le=1)
    positive_total: int = Field(ge=0)
    executed_positive_total: int = Field(ge=0)
    positive_execution_pass_rate: float = Field(ge=0, le=1)
    baseline_matched_positive_total: int = Field(ge=0)
    positive_baseline_match_rate: float = Field(ge=0, le=1)
    positive_passed: int = Field(ge=0)
    positive_pass_rate: float = Field(ge=0, le=1)
    by_category: dict[str, CheckMetric]
    by_difficulty: dict[str, CheckMetric]
    by_outcome: dict[str, CheckMetric]
    failed_checks: dict[str, int]
    by_check: dict[str, CheckMetric]


class EvaluationThresholds(EvidenceModel):
    min_pass_rate: float = Field(ge=0, le=1)
    min_refusal_pass_rate: float = Field(ge=0, le=1)
    min_positive_pass_rate: float = Field(ge=0, le=1)
    min_cases: int = Field(ge=0)
    min_positive_cases: int = Field(ge=0)
    min_refusal_cases: int = Field(ge=0)
    min_semantic_ir_pass_rate: float = Field(ge=0, le=1)
    min_execution_pass_rate: float = Field(ge=0, le=1)
    min_retrieval_recall: float = Field(ge=0, le=1)


class EvaluationQualityGate(EvidenceModel):
    passed: bool
    failures: list[str]
    thresholds: EvaluationThresholds


class EvaluationReport(EvidenceModel):
    schema_version: Literal[2]
    generated_at: str
    split: Literal["dev", "test"]
    attestation: EvaluationAttestation
    summary: dict[str, Any]
    quality_gate: dict[str, Any]
    results: list[EvaluationResult] = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def strict_summary(cls, value: dict[str, Any]) -> dict[str, Any]:
        EvaluationSummary.model_validate(value)
        return value

    @field_validator("quality_gate")
    @classmethod
    def strict_gate(cls, value: dict[str, Any]) -> dict[str, Any]:
        EvaluationQualityGate.model_validate(value)
        return value

    @field_validator("generated_at")
    @classmethod
    def timestamp_is_aware(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("generated_at must contain a timezone")
        return value


def _case_errors(case: EvaluationCase, result: EvaluationResult, index: int) -> list[str]:
    errors: list[str] = []
    actual: Any
    payload = case.payload
    if (
        case.split == "test"
        and not payload["should_refuse"]
        and payload.get("must_execute") is not True
    ):
        errors.append(f"{case.id}: frozen positive cases must explicitly require execution")
    for field, expected in (
        ("split", case.split),
        ("question", case.question),
        ("case_index", index),
        ("category", str(payload.get("category") or "")),
        ("difficulty", str(payload.get("difficulty") or "")),
        ("should_refuse", payload["should_refuse"]),
    ):
        if getattr(result, field) != expected:
            errors.append(f"{case.id}: {field} differs from the frozen case")
    checks = {item.name: item for item in result.checks}
    if len(checks) != len(result.checks):
        errors.append(f"{case.id}: duplicate check names")
    required: set[str] = {"outcome"}
    target = expected_outcome(payload)
    expected_outcome_pass = result.outcome == target
    if result.should_refuse:
        required.add("refusal_evidence")
        expected_outcome_pass = expected_outcome_pass and bool(result.refusal_reason)
    for name, expected, actual, passed in (
        ("outcome", target, result.outcome, expected_outcome_pass),
        (
            "refusal_evidence",
            True,
            bool(result.refusal_reason) and not result.actual_sql and not result.executed,
            bool(result.refusal_reason) and not result.actual_sql and not result.executed,
        ),
    ):
        if name not in required:
            continue
        item = checks.get(name)
        if item and (
            item.expected != expected or item.actual != actual or item.passed is not passed
        ):
            errors.append(f"{case.id}: inconsistent {name} evidence")

    assertions = SqlAssertions(result.actual_sql)
    for structural in assertions.checks(payload):
        name = structural["name"]
        required.add(name)
        item = checks.get(name)
        if item and (
            item.expected != structural["expected"] or item.passed is not structural["passed"]
        ):
            errors.append(f"{case.id}: SQL AST assertion was altered: {name}")
    expected_ir = payload.get("expected_semantic_ir") or {}
    if case.split == "test" and validate_semantic_snapshot(expected_ir):
        errors.append(f"{case.id}: frozen cases require complete current semantic expectations")
    for key, expected in expected_ir.items():
        name = f"semantic_ir:{key}"
        required.add(name)
        item = checks.get(name)
        if item and (item.expected != expected or item.passed is not (item.actual == expected)):
            errors.append(f"{case.id}: inconsistent semantic check: {key}")
    for column in payload.get("expected_result_columns", []):
        name = f"result_column:{column}"
        required.add(name)
        item = checks.get(name)
        if item and (
            item.expected != column
            or item.actual != result.result_columns
            or item.passed is not (column in result.result_columns)
        ):
            errors.append(f"{case.id}: inconsistent result column evidence")
    for name, test in (
        ("min_result_rows", lambda value: result.result_row_count >= value),
        ("max_result_rows", lambda value: result.result_row_count <= value),
    ):
        if name in payload:
            required.add(name)
            item = checks.get(name)
            if item and (
                item.expected != payload[name]
                or item.actual != result.result_row_count
                or item.passed is not test(payload[name])
            ):
                errors.append(f"{case.id}: inconsistent row count evidence")
    if not result.should_refuse:
        required.update({"sql_generated", "sql_parse", "retrieval_recall"})
        expected_tables = sorted({name.lower() for name in payload.get("must_include_tables", [])})
        candidates = sorted(result.candidate_tables)
        for name, expected, actual, passed in (
            ("sql_generated", "non-empty sql", bool(result.actual_sql), bool(result.actual_sql)),
            (
                "sql_parse",
                "single T-SQL query",
                assertions.tree is not None,
                assertions.tree is not None,
            ),
            (
                "retrieval_recall",
                expected_tables,
                candidates,
                set(expected_tables).issubset(candidates),
            ),
        ):
            item = checks.get(name)
            if item and (
                item.expected != expected or item.actual != actual or item.passed is not passed
            ):
                errors.append(f"{case.id}: inconsistent {name} evidence")
        if payload.get("must_execute", True):
            required.update(
                {
                    "execution_success",
                    "baseline_execution_success",
                    "baseline_result_complete",
                    "baseline_result_columns",
                    "baseline_result_row_count",
                    "baseline_result_match",
                }
            )
            execution = checks.get("execution_success")
            if execution and (
                execution.expected is not True
                or execution.actual is not result.executed
                or execution.passed is not result.executed
            ):
                errors.append(f"{case.id}: inconsistent execution evidence")
            complete = checks.get("baseline_result_complete")
            if complete and (
                complete.expected is not True
                or complete.actual is not result.baseline_complete
                or complete.passed is not result.baseline_complete
            ):
                errors.append(f"{case.id}: inconsistent complete-result evidence")
            if result.baseline_complete and (
                not result.executed
                or not result.baseline_compared
                or result.result_truncated
                or result.baseline_error
            ):
                errors.append(f"{case.id}: incomplete results cannot attest a baseline comparison")
            for name, expected, actual, passed in (
                (
                    "baseline_execution_success",
                    True,
                    result.baseline_succeeded,
                    result.baseline_succeeded,
                ),
                (
                    "baseline_result_columns",
                    result.baseline_result_columns,
                    result.result_columns,
                    result.baseline_complete
                    and result.baseline_result_columns == result.result_columns,
                ),
                (
                    "baseline_result_row_count",
                    result.baseline_result_row_count,
                    result.result_row_count,
                    result.baseline_complete
                    and result.baseline_result_row_count == result.result_row_count,
                ),
            ):
                item = checks.get(name)
                if item and (
                    item.expected != expected or item.actual != actual or item.passed is not passed
                ):
                    errors.append(f"{case.id}: inconsistent {name} evidence")
            if result.baseline_succeeded and (
                not result.baseline_compared or result.baseline_error
            ):
                errors.append(f"{case.id}: invalid baseline execution evidence")
            if result.baseline_complete and not result.baseline_succeeded:
                errors.append(f"{case.id}: failed baseline cannot attest complete results")
            match = checks.get("baseline_result_match")
            if match and (
                match.expected != ResultComparison.from_case(payload).to_dict()
                or not isinstance(match.actual, dict)
                or match.actual.get("complete") is not result.baseline_complete
                or match.actual.get("matched") is not match.passed
                or match.passed
                and not result.baseline_complete
            ):
                errors.append(f"{case.id}: inconsistent baseline comparison evidence")
    missing = required - set(checks)
    unexpected = set(checks) - required
    if missing:
        errors.append(f"{case.id}: missing required checks: {', '.join(sorted(missing))}")
    if unexpected:
        errors.append(f"{case.id}: unexpected checks: {', '.join(sorted(unexpected))}")
    passed = all(item.passed for item in result.checks)
    if result.passed is not passed:
        errors.append(f"{case.id}: passed flag contradicts per-case checks")
    return errors


def validate_evaluation_report(
    payload: dict[str, Any],
    cases: list[EvaluationCase],
    *,
    expected_split: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate every result against its frozen case before recomputing release metrics."""
    try:
        report = EvaluationReport.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        message = str(exc).splitlines()[0]
        return [], [f"evaluation report schema invalid: {message}"]
    errors: list[str] = []
    if report.split != expected_split:
        errors.append("evaluation report split differs from the requested frozen split")
    actual_ids = [item.id for item in report.results]
    expected_ids = {case.id for case in cases}
    if len(actual_ids) != len(set(actual_ids)):
        errors.append("evaluation report contains duplicate case IDs")
    if set(actual_ids) != expected_ids or len(actual_ids) != len(cases):
        errors.append("evaluation report must cover every frozen case ID exactly once")
    by_id = {item.id: item for item in report.results}
    for index, case in enumerate(cases, 1):
        result = by_id.get(case.id)
        if result is not None:
            errors.extend(_case_errors(case, result, index))
    return [item.model_dump() for item in report.results], errors
