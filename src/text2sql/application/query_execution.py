"""Execute one validated query, preserving typed evaluation evidence."""

from __future__ import annotations

from typing import Any

from ..core.logging import setup_logging
from ..domain.result_contract import read_tabular_result
from ..domain.sql_validation import limit_tsql_rows
from .contracts import QueryContext
from .ports import Repository, SqlExecutor
from .query_config import QueryServiceConfig
from .query_response import build_query_response

logger = setup_logging("text2sql.execution")


async def execute_query(
    question: str,
    sql: str,
    context: QueryContext,
    *,
    executor: SqlExecutor,
    repository: Repository,
    config: QueryServiceConfig,
    attempts: int,
    capture_feedback: bool,
    preserve_result_types: bool,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "question": question,
        "attempts": attempts,
        "sql": sql,
        "candidate_tables": context.candidate_tables,
        "candidate_scores": context.candidate_scores,
        "candidate_score_reasons": context.candidate_score_reasons,
        "diagnostics": context.diagnostics,
    }
    try:
        result = await executor.execute(
            limit_tsql_rows(sql, config.max_result_rows + 1),
            timeout_seconds=config.query_timeout_seconds,
        )
    except Exception as exc:
        logger.warning("SQL execution failed: %s", type(exc).__name__)
        return build_query_response(
            success=False,
            outcome="infrastructure_error",
            error_code="SQL_EXECUTION_FAILED",
            error="SQL 执行失败，请检查数据库连接、权限及超时",
            **common,
        )
    try:
        admitted = read_tabular_result(result)
    except ValueError:
        return build_query_response(
            success=False,
            outcome="infrastructure_error",
            error_code="EXECUTOR_CONTRACT_INVALID",
            error="数据库执行器返回了无效结果",
            **common,
        )
    columns, payload = admitted.columns, admitted.rows
    observed_rows = len(payload)
    if capture_feedback:
        try:
            await repository.capture(
                question,
                sql,
                context.candidate_tables,
                execution_succeeded=True,
                result_row_count=observed_rows,
                approved=observed_rows >= config.feedback_min_result_rows,
                capture_source="execution",
            )
        except Exception as exc:
            logger.warning("反馈候选写入失败: %s", type(exc).__name__)
    truncated = observed_rows > config.max_result_rows
    return build_query_response(
        success=True,
        result=payload[: config.max_result_rows],
        result_columns=columns,
        result_truncated=truncated,
        result_total_rows=observed_rows,
        preserve_result_types=preserve_result_types,
        **common,
    )
