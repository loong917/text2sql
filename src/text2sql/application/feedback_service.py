"""Validate review submissions before they can modify trusted knowledge."""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.config import Settings
from ..domain.result_contract import read_tabular_result
from ..domain.semantic_ir import parse_question_semantics
from ..domain.sql_validation import limit_tsql_rows
from ..knowledge.provenance import schema_fingerprint
from .context_service import validate_sql
from .ports import ArtifactProvider, FeedbackRepository, SchemaRepository, SqlExecutor
from .sql_policy import SqlValidationConfig


async def review_feedback(
    *,
    question: str,
    sql: str,
    validation_label: str,
    reviewer: str,
    candidate_tables: list[str],
    candidate_score_reasons: dict[str, Any] | None,
    comment: str = "",
    result_row_count: int = 0,
    had_execution_result: bool = False,
    config: Settings,
    sql_executor: SqlExecutor,
    feedback_repository: FeedbackRepository,
    schema_repository: SchemaRepository,
    artifact_provider: ArtifactProvider,
) -> dict[str, Any]:
    """Validate a positive review against current Schema and semantic intent."""
    evidence: dict[str, Any] | None = None
    verified_result_row_count = 0
    execution_validated = False
    if validation_label.strip().lower() == "correct":
        sql_validation_config = SqlValidationConfig(
            allowed_schemas=config.sql_allowed_schemas,
            max_joins=config.sql_max_joins,
            max_subqueries=config.sql_max_subqueries,
            allowed_tables=config.sql_allowed_tables,
            denied_tables=config.sql_denied_tables,
            denied_columns=config.sql_denied_columns,
            aggregation_only_tables=config.sql_aggregation_only_tables,
            allow_select_star=config.sql_allow_select_star,
            require_table=config.sql_require_table,
            allow_cross_join=config.sql_allow_cross_join,
        )
        schema = (await schema_repository.get()).tables
        knowledge = artifact_provider.get()
        if knowledge.schema_fingerprint != schema_fingerprint(schema):
            raise ValueError("知识产物与当前 Schema 不一致，禁止晋升 Gold")
        semantic_ir = parse_question_semantics(question, knowledge.catalog)
        if semantic_ir.date_is_relative:
            raise ValueError("相对日期问题不能晋升 Gold，请将日期改为明确的绝对日期后重新审核")
        validation_error = validate_sql(
            sql,
            schema,
            question=question,
            semantic_ir=semantic_ir,
            config=sql_validation_config,
        )
        evidence = {
            "ast_validated": validation_error is None,
            "schema_validated": validation_error is None,
            "semantic_validated": validation_error is None,
            "execution_validated": bool(had_execution_result),
            "schema_fingerprint": schema_fingerprint(schema),
            "validation_error": validation_error,
        }
        if validation_error:
            raise ValueError(f"反馈 SQL 未通过晋升门禁: {validation_error}")
        execution_result = await sql_executor.execute(
            limit_tsql_rows(sql, config.max_result_rows + 1),
            timeout_seconds=config.sql_query_timeout_seconds,
        )
        try:
            rows = read_tabular_result(execution_result).rows
        except ValueError as exc:
            raise ValueError("反馈执行结果不符合 SQL Executor 契约，禁止晋升 Gold") from exc
        verified_result_row_count = len(rows)
        execution_validated = True
        evidence["execution_validated"] = True

    return await asyncio.to_thread(
        feedback_repository.submit_review,
        question=question,
        sql=sql,
        candidate_tables=candidate_tables,
        candidate_score_reasons=candidate_score_reasons,
        validation_label=validation_label,
        comment=comment,
        result_row_count=(
            verified_result_row_count
            if validation_label.strip().lower() == "correct"
            else result_row_count
        ),
        had_execution_result=(
            execution_validated
            if validation_label.strip().lower() == "correct"
            else had_execution_result
        ),
        reviewer=reviewer,
        promotion_evidence=evidence,
    )
