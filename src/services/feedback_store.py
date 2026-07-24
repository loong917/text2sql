"""Tiered feedback persistence.

Execution results are pending candidates. Only user-confirmed records become
Gold examples; rejected records are retained as negative examples.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any

from ..core.config import settings
from ..core.logging import setup_logging

logger = setup_logging("text2sql.feedback_store")
_file_lock = threading.Lock()

ERROR_TYPES = re.compile(
    r"wrong_table|missing_filter|wrong_metric|wrong_dimension|wrong_join|"
    r"wrong_granularity|wrong_entity_value",
    flags=re.I,
)


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip()


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("跳过损坏的反馈记录: %s", line[:120])
            continue
        if payload.get("question") and payload.get("sql"):
            records.append(payload)
    return records


def _keywords(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(r"[a-zA-Z_]\w+|[\u4e00-\u9fff]{2,}", text.lower())
        )
    )


def load_gold_examples() -> list[dict[str, Any]]:
    """Return trusted examples suitable for training and few-shot use."""
    examples: list[dict[str, Any]] = []
    for payload in _merge_records(_read_records(Path(settings.feedback_examples_path))):
        user_validated = bool(payload.get("user_validated")) or (
            str(payload.get("capture_source") or "") == "online_validation"
        )
        if str(payload.get("feedback_tier") or "").lower() != "gold" and not user_validated:
            continue
        if payload.get("approved") is False:
            continue
        if int(payload.get("quality_score", 0) or 0) < settings.feedback_min_quality_score:
            continue
        if not user_validated:
            if settings.feedback_require_execution_success and not payload.get(
                "execution_succeeded", False
            ):
                continue
            if (
                settings.feedback_require_nonempty_result
                and int(payload.get("result_row_count", 0) or 0)
                < settings.feedback_min_result_rows
            ):
                continue
        examples.append(payload)
    return examples


def search_gold_examples(question: str, limit: int) -> list[dict[str, str]]:
    if limit <= 0:
        return []
    keywords = _keywords(question)
    scored: list[tuple[int, dict[str, Any]]] = []
    for payload in load_gold_examples():
        example_question = str(payload.get("question") or "")
        overlap = sum(1 for keyword in keywords if keyword in example_question.lower())
        if overlap:
            score = overlap * 10 + int(payload.get("quality_score", 0) or 0) // 10
            scored.append((score, payload))
    scored.sort(key=lambda item: -item[0])
    return [
        {"question": str(item["question"]), "sql": str(item["sql"])}
        for _, item in scored[:limit]
    ]


def search_negative_examples(question: str, limit: int = 2) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    keywords = _keywords(question)
    scored: list[tuple[int, dict[str, Any]]] = []
    for payload in _read_records(Path(settings.feedback_negative_path)):
        example_question = str(payload.get("question") or "")
        overlap = sum(1 for keyword in keywords if keyword in example_question.lower())
        if overlap:
            scored.append((overlap, payload))
    scored.sort(key=lambda item: -item[0])
    return [payload for _, payload in scored[:limit]]


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(item, ensure_ascii=False) for item in records)
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def _merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in records:
        question = normalize_question(str(item.get("question") or ""))
        sql = normalize_sql(str(item.get("sql") or ""))
        if not question or not sql:
            continue
        signature = f"{question.lower()}\n{sql.lower()}"
        payload = {
            **item,
            "question": question,
            "sql": sql,
            "candidate_tables": _dedupe(
                [str(value) for value in item.get("candidate_tables", []) or []]
            ),
            "quality_flags": _dedupe(
                [str(value) for value in item.get("quality_flags", []) or []]
            ),
        }
        if signature not in merged:
            merged[signature] = payload
            order.append(signature)
            continue
        current = merged[signature]
        current["candidate_tables"] = _dedupe(
            [*current.get("candidate_tables", []), *payload["candidate_tables"]]
        )
        current["quality_flags"] = _dedupe(
            [*current.get("quality_flags", []), *payload["quality_flags"]]
        )
        current["execution_succeeded"] = bool(
            current.get("execution_succeeded") or payload.get("execution_succeeded")
        )
        current["user_validated"] = bool(
            current.get("user_validated") or payload.get("user_validated")
        )
        current["result_row_count"] = max(
            int(current.get("result_row_count", 0) or 0),
            int(payload.get("result_row_count", 0) or 0),
        )
        current["quality_score"] = max(
            int(current.get("quality_score", 0) or 0),
            int(payload.get("quality_score", 0) or 0),
        )
    return [merged[key] for key in order]


def _eligible(
    *, execution_succeeded: bool, result_row_count: int, approved: bool, user_validated: bool
) -> tuple[bool, list[str]]:
    if user_validated and approved:
        return True, []
    reasons: list[str] = []
    if settings.feedback_require_execution_success and not execution_succeeded:
        reasons.append("execution_failed")
    if (
        settings.feedback_require_nonempty_result
        and result_row_count < settings.feedback_min_result_rows
    ):
        reasons.append("result_too_small")
    if not approved:
        reasons.append("not_approved")
    return not reasons, reasons


def _upsert(
    *,
    path: Path,
    tier: str,
    question: str,
    sql: str,
    candidate_tables: list[str],
    execution_succeeded: bool,
    result_row_count: int,
    approved: bool,
    capture_source: str,
    user_validated: bool,
    quality_score: int,
) -> bool:
    eligible, reasons = _eligible(
        execution_succeeded=execution_succeeded,
        result_row_count=result_row_count,
        approved=approved,
        user_validated=user_validated,
    )
    if not eligible:
        logger.info("跳过低质量反馈: %s (%s)", question, ",".join(reasons))
        return False
    payload = {
        "question": normalize_question(question),
        "sql": normalize_sql(sql),
        "candidate_tables": _dedupe(candidate_tables),
        "execution_succeeded": execution_succeeded,
        "result_row_count": max(0, int(result_row_count)),
        "approved": approved,
        "capture_source": capture_source,
        "user_validated": user_validated,
        "quality_score": max(0, min(100, int(quality_score))),
        "quality_flags": reasons,
        "feedback_tier": tier,
        "captured_at": _now(),
    }
    with _file_lock:
        records = _merge_records([*_read_records(path), payload])
        _write_records(path, records)
    return True


def _remove_question(path: Path, question: str, keep_sql: str = "") -> None:
    normalized_question = normalize_question(question).lower()
    normalized_keep_sql = normalize_sql(keep_sql).lower()
    with _file_lock:
        records = _read_records(path)
        kept = [
            item
            for item in records
            if normalize_question(str(item.get("question") or "")).lower()
            != normalized_question
            or (
                normalized_keep_sql
                and normalize_sql(str(item.get("sql") or "")).lower()
                == normalized_keep_sql
            )
        ]
        if len(kept) != len(records):
            _write_records(path, kept)


def capture_execution_feedback_sync(
    question: str,
    sql: str,
    candidate_tables: list[str],
    *,
    execution_succeeded: bool,
    result_row_count: int,
    approved: bool = True,
    capture_source: str = "execution",
) -> bool:
    quality_score = min(
        100,
        50
        + (25 if execution_succeeded else 0)
        + (15 if result_row_count >= settings.feedback_min_result_rows else 0)
        + (10 if approved else 0),
    )
    return _upsert(
        path=Path(settings.feedback_pending_path),
        tier="pending",
        question=question,
        sql=sql,
        candidate_tables=candidate_tables,
        execution_succeeded=execution_succeeded,
        result_row_count=result_row_count,
        approved=approved,
        capture_source=capture_source,
        user_validated=False,
        quality_score=quality_score,
    )


async def capture_execution_feedback(
    question: str,
    sql: str,
    candidate_tables: list[str],
    *,
    execution_succeeded: bool,
    result_row_count: int,
    approved: bool = True,
    capture_source: str = "execution",
) -> bool:
    if not settings.enable_feedback_capture:
        return False
    return await asyncio.to_thread(
        capture_execution_feedback_sync,
        question,
        sql,
        candidate_tables,
        execution_succeeded=execution_succeeded,
        result_row_count=result_row_count,
        approved=approved,
        capture_source=capture_source,
    )


def _append(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock, path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def submit_online_feedback(
    *,
    question: str,
    sql: str,
    candidate_tables: list[str],
    candidate_score_reasons: dict[str, Any] | None,
    validation_label: str,
    comment: str = "",
    result_row_count: int = 0,
    had_execution_result: bool = False,
) -> dict[str, Any]:
    label = validation_label.strip().lower()
    if label not in {"correct", "incorrect"}:
        raise ValueError("validation_label 必须是 correct 或 incorrect")
    question, sql, comment = (
        normalize_question(question),
        normalize_sql(sql),
        comment.strip(),
    )
    if not question or not sql:
        raise ValueError("question 和 sql 不能为空")

    review = {
        "question": question,
        "sql": sql,
        "candidate_tables": _dedupe(candidate_tables),
        "candidate_score_reasons": candidate_score_reasons or {},
        "validation_label": label,
        "comment": comment,
        "result_row_count": max(0, int(result_row_count)),
        "had_execution_result": bool(had_execution_result),
        "submitted_at": _now(),
    }
    _append(Path(settings.feedback_review_path), review)

    captured = False
    if label == "incorrect":
        _append(
            Path(settings.feedback_negative_path),
            {
                **review,
                "feedback_tier": "negative",
                "error_types": _dedupe(ERROR_TYPES.findall(comment)),
            },
        )
    elif settings.enable_feedback_capture:
        _remove_question(Path(settings.feedback_pending_path), question)
        _remove_question(Path(settings.feedback_examples_path), question, sql)
        captured = _upsert(
            path=Path(settings.feedback_examples_path),
            tier="gold",
            question=question,
            sql=sql,
            candidate_tables=candidate_tables,
            execution_succeeded=bool(had_execution_result or result_row_count > 0),
            result_row_count=max(
                int(result_row_count),
                settings.feedback_min_result_rows if had_execution_result else 0,
            ),
            approved=True,
            capture_source="online_validation",
            user_validated=True,
            quality_score=max(settings.feedback_min_quality_score, 95),
        )

    return {
        "success": True,
        "validation_label": label,
        "feedback_captured": captured,
        "submitted_at": review["submitted_at"],
    }
