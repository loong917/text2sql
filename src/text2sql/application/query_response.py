"""Serialize the stable response contract of the Text2SQL use case."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from .contracts import QueryOutcome


def make_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = value.tobytes() if isinstance(value, memoryview) else bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def build_query_response(
    *,
    success: bool,
    question: str,
    attempts: int,
    sql: Any = None,
    result: Any = None,
    error: Any = None,
    candidate_tables: list[str] | None = None,
    candidate_scores: dict[str, float] | None = None,
    candidate_score_reasons: dict[str, Any] | None = None,
    refusal_reason: str | None = None,
    result_truncated: bool = False,
    result_total_rows: int | None = None,
    outcome: QueryOutcome | None = None,
    error_code: str | None = None,
    diagnostics: dict[str, Any] | None = None,
    preserve_result_types: bool = False,
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    rows = result if isinstance(result, list) else None
    returned_rows = len(rows) if rows is not None else 0
    return {
        "success": success,
        "outcome": outcome
        or ("success" if success else "refused" if refusal_reason else "generation_failed"),
        "error_code": error_code,
        "question": question,
        "sql": make_json_safe(sql),
        "result": result if preserve_result_types else make_json_safe(result),
        "attempts": attempts,
        "error": make_json_safe(error),
        "candidate_tables": make_json_safe(candidate_tables or []),
        "candidate_scores": make_json_safe(candidate_scores or {}),
        "candidate_score_reasons": make_json_safe(candidate_score_reasons or {}),
        "refusal_reason": make_json_safe(refusal_reason),
        "result_row_count": returned_rows,
        "result_total_rows": result_total_rows if result_total_rows is not None else returned_rows,
        "result_truncated": result_truncated,
        "result_columns": result_columns
        if result_columns is not None
        else list(rows[0])
        if rows
        else [],
        "diagnostics": diagnostics or {},
    }
