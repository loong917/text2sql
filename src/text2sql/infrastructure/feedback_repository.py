"""Transactional SQLite repository for governed Text2SQL feedback."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .feedback_migrations import migrate_feedback_schema

ERROR_TYPES = re.compile(
    r"wrong_table|missing_filter|wrong_metric|wrong_dimension|wrong_join|"
    r"wrong_granularity|wrong_entity_value",
    flags=re.I,
)


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip()


def normalize_sql(sql: str) -> str:
    """Preserve reviewed SQL literals and line-comment boundaries verbatim."""
    return sql.strip()


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _keywords(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[a-zA-Z_]\w+|[\u4e00-\u9fff]{2,}", text.lower())))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


@dataclass(frozen=True)
class FeedbackPolicy:
    enabled: bool = True
    require_execution_success: bool = True
    require_nonempty_result: bool = True
    min_result_rows: int = 1
    min_quality_score: int = 75


class SQLiteFeedbackRepository:
    """Persist feedback tiers and review audits with transactional promotion."""

    def __init__(self, db_path: str | Path, policy: FeedbackPolicy | None = None):
        self._path = Path(db_path)
        self._policy = policy or FeedbackPolicy()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            migrate_feedback_schema(connection)

    @staticmethod
    def _signature(question: str, sql: str) -> str:
        # SQL case can be significant inside literals or under a case-sensitive
        # collation. False-negative deduplication is safer than merging queries.
        return f"{normalize_question(question).lower()}\n{normalize_sql(sql)}"

    @staticmethod
    def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "question": row["question"],
            "sql": row["sql"],
            "candidate_tables": _decode(row["candidate_tables_json"], []),
            "candidate_score_reasons": _decode(row["candidate_score_reasons_json"], {}),
            "promotion_evidence": _decode(row["promotion_evidence_json"], {}),
            "quality_flags": _decode(row["quality_flags_json"], []),
            "error_types": _decode(row["error_types_json"], []),
            "execution_succeeded": bool(row["execution_succeeded"]),
            "result_row_count": int(row["result_row_count"]),
            "approved": bool(row["approved"]),
            "user_validated": bool(row["user_validated"]),
            "quality_score": int(row["quality_score"]),
            "capture_source": row["capture_source"],
            "feedback_tier": row["tier"],
            "status": row["status"],
            "reviewer": row["reviewer"],
            "captured_at": row["captured_at"],
            "updated_at": row["updated_at"],
        }

    def _upsert_sample(
        self,
        connection: sqlite3.Connection,
        *,
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
        quality_flags: list[str] | None = None,
        error_types: list[str] | None = None,
        reviewer: str = "",
        promotion_evidence: dict[str, Any] | None = None,
        candidate_score_reasons: dict[str, Any] | None = None,
    ) -> None:
        question = normalize_question(question)
        sql = normalize_sql(sql)
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO feedback_samples (
                signature, tier, question, sql, candidate_tables_json,
                candidate_score_reasons_json, promotion_evidence_json,
                quality_flags_json, error_types_json, execution_succeeded,
                result_row_count, approved, user_validated, quality_score,
                capture_source, status, reviewer, captured_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signature) DO UPDATE SET
                tier=excluded.tier,
                candidate_tables_json=excluded.candidate_tables_json,
                candidate_score_reasons_json=excluded.candidate_score_reasons_json,
                promotion_evidence_json=excluded.promotion_evidence_json,
                quality_flags_json=excluded.quality_flags_json,
                error_types_json=excluded.error_types_json,
                execution_succeeded=MAX(feedback_samples.execution_succeeded,
                                        excluded.execution_succeeded),
                result_row_count=MAX(feedback_samples.result_row_count,
                                     excluded.result_row_count),
                approved=excluded.approved,
                user_validated=excluded.user_validated,
                quality_score=MAX(feedback_samples.quality_score, excluded.quality_score),
                capture_source=excluded.capture_source,
                status=excluded.status,
                reviewer=excluded.reviewer,
                updated_at=excluded.updated_at
            """,
            (
                self._signature(question, sql),
                tier,
                question,
                sql,
                _json(_dedupe(candidate_tables)),
                _json(candidate_score_reasons or {}),
                _json(promotion_evidence or {}),
                _json(_dedupe(quality_flags or [])),
                _json(_dedupe(error_types or [])),
                int(execution_succeeded),
                max(0, int(result_row_count)),
                int(approved),
                int(user_validated),
                max(0, min(100, int(quality_score))),
                capture_source,
                tier,
                reviewer,
                timestamp,
                timestamp,
            ),
        )

    def capture_sync(
        self,
        question: str,
        sql: str,
        candidate_tables: list[str],
        *,
        execution_succeeded: bool,
        result_row_count: int,
        approved: bool = True,
        capture_source: str = "execution",
        **_: Any,
    ) -> bool:
        if not self._policy.enabled:
            return False
        reasons: list[str] = []
        if self._policy.require_execution_success and not execution_succeeded:
            reasons.append("execution_failed")
        if self._policy.require_nonempty_result and result_row_count < self._policy.min_result_rows:
            reasons.append("result_too_small")
        if not approved:
            reasons.append("not_approved")
        if reasons:
            return False
        quality_score = min(
            100,
            50
            + (25 if execution_succeeded else 0)
            + (15 if result_row_count >= self._policy.min_result_rows else 0)
            + (10 if approved else 0),
        )
        with closing(self._connect()) as connection, connection:
            self._upsert_sample(
                connection,
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
        return True

    async def capture(
        self,
        question: str,
        sql: str,
        candidate_tables: list[str],
        **evidence: Any,
    ) -> bool:
        return await asyncio.to_thread(
            self.capture_sync, question, sql, candidate_tables, **evidence
        )

    def list_samples(self, tier: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM feedback_samples WHERE tier = ? ORDER BY updated_at DESC", (tier,)
            ).fetchall()
        return [self._row_payload(row) for row in rows]

    def count(self, tier: str) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM feedback_samples WHERE tier = ?", (tier,)
            ).fetchone()
        return int(row["total"]) if row else 0

    def load_gold(self, *, current_schema_fingerprint: str | None = None) -> list[dict[str, Any]]:
        examples: list[dict[str, Any]] = []
        for payload in self.list_samples("gold"):
            evidence = payload["promotion_evidence"]
            if not all(
                bool(evidence.get(key))
                for key in (
                    "ast_validated",
                    "schema_validated",
                    "semantic_validated",
                    "execution_validated",
                )
            ):
                continue
            if current_schema_fingerprint and str(evidence.get("schema_fingerprint") or "") != str(
                current_schema_fingerprint
            ):
                continue
            if payload["quality_score"] < self._policy.min_quality_score:
                continue
            examples.append(payload)
        return examples

    def search_gold(
        self,
        question: str,
        limit: int,
        *,
        current_schema_fingerprint: str | None = None,
    ) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        keywords = _keywords(question)
        scored: list[tuple[int, dict[str, Any]]] = []
        for payload in self.load_gold(current_schema_fingerprint=current_schema_fingerprint):
            candidate = str(payload["question"]).lower()
            overlap = sum(1 for keyword in keywords if keyword in candidate)
            if overlap:
                scored.append((overlap * 10 + payload["quality_score"] // 10, payload))
        scored.sort(key=lambda item: -item[0])
        return [{"question": item["question"], "sql": item["sql"]} for _, item in scored[:limit]]

    def search_negative(self, question: str, limit: int = 2) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        keywords = _keywords(question)
        scored: list[tuple[int, dict[str, Any]]] = []
        for payload in self.list_samples("negative"):
            candidate = str(payload["question"]).lower()
            overlap = sum(1 for keyword in keywords if keyword in candidate)
            if overlap:
                scored.append((overlap, payload))
        scored.sort(key=lambda item: -item[0])
        return [payload for _, payload in scored[:limit]]

    def submit_review(
        self,
        *,
        question: str,
        sql: str,
        candidate_tables: list[str],
        candidate_score_reasons: dict[str, Any] | None,
        validation_label: str,
        comment: str = "",
        result_row_count: int = 0,
        had_execution_result: bool = False,
        reviewer: str = "unknown",
        promotion_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        label = validation_label.strip().lower()
        if label not in {"correct", "incorrect"}:
            raise ValueError("validation_label 必须是 correct 或 incorrect")
        question, sql = normalize_question(question), normalize_sql(sql)
        if not question or not sql:
            raise ValueError("question 和 sql 不能为空")
        if label == "correct" and not all(
            bool((promotion_evidence or {}).get(key))
            for key in (
                "ast_validated",
                "schema_validated",
                "semantic_validated",
                "execution_validated",
            )
        ):
            raise ValueError("正确反馈必须通过 AST、Schema、语义和执行晋升门禁")

        timestamp = _now()
        reviewer_value = reviewer.strip() or "unknown"
        review: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "question": question,
            "sql": sql,
            "candidate_tables": _dedupe(candidate_tables),
            "candidate_score_reasons": candidate_score_reasons or {},
            "validation_label": label,
            "comment": comment.strip(),
            "result_row_count": max(0, int(result_row_count)),
            "had_execution_result": bool(had_execution_result),
            "reviewer": reviewer_value,
            "promotion_evidence": promotion_evidence or {},
            "submitted_at": timestamp,
        }
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO feedback_reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    review["id"],
                    question,
                    sql,
                    label,
                    reviewer_value,
                    _json(review),
                    timestamp,
                ),
            )
            tier = "negative" if label == "incorrect" else "gold"
            if tier == "gold":
                connection.execute(
                    "DELETE FROM feedback_samples WHERE tier = 'pending' AND lower(question) = ?",
                    (question.lower(),),
                )
            self._upsert_sample(
                connection,
                tier=tier,
                question=question,
                sql=sql,
                candidate_tables=candidate_tables,
                execution_succeeded=bool(had_execution_result),
                result_row_count=result_row_count,
                approved=label == "correct",
                capture_source="online_validation",
                user_validated=label == "correct",
                quality_score=max(self._policy.min_quality_score, 95) if label == "correct" else 0,
                error_types=ERROR_TYPES.findall(comment) if label == "incorrect" else [],
                reviewer=reviewer_value,
                promotion_evidence=promotion_evidence,
                candidate_score_reasons=candidate_score_reasons,
            )
        return {
            "success": True,
            "validation_label": label,
            "feedback_captured": True,
            "submitted_at": timestamp,
        }

    def submit_candidate_review(
        self,
        *,
        question: str,
        sql: str,
        validation_label: str,
        candidate_tables: list[str],
        candidate_score_reasons: dict[str, Any] | None,
        comment: str = "",
        result_row_count: int = 0,
        had_execution_result: bool = False,
    ) -> dict[str, Any]:
        """Record untrusted user feedback for later administrative review."""
        label = validation_label.strip().lower()
        question, sql = normalize_question(question), normalize_sql(sql)
        if label not in {"correct", "incorrect"}:
            raise ValueError("validation_label 必须是 correct 或 incorrect")
        if not question or not sql:
            raise ValueError("question 和 sql 不能为空")
        timestamp = _now()
        review_id = str(uuid.uuid4())
        payload = {
            "id": review_id,
            "question": question,
            "sql": sql,
            "validation_label": label,
            "candidate_tables": _dedupe(candidate_tables),
            "candidate_score_reasons": candidate_score_reasons or {},
            "comment": comment.strip(),
            "result_row_count": max(0, int(result_row_count)),
            "had_execution_result": bool(had_execution_result),
            "reviewer": "end-user",
            "status": "pending_review",
            "submitted_at": timestamp,
        }
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO feedback_reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    question,
                    sql,
                    label,
                    "end-user",
                    _json(payload),
                    timestamp,
                ),
            )
        return {
            "success": True,
            "validation_label": label,
            "feedback_captured": False,
            "status": "pending_review",
            "submitted_at": timestamp,
        }
