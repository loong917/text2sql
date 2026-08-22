"""Online Text2SQL orchestration over explicit application ports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.logging import setup_logging
from ..domain.sql_validation import limit_tsql_rows
from .constants import REFUSAL_TOKEN
from .ports import Generator, Repository, Retriever, SqlExecutor, Validator
from .query_config import QueryServiceConfig
from .query_prompt import build_generation_prompt
from .query_response import build_query_response

logger = setup_logging("text2sql.application")


def _normalize_sql_output(sql: str) -> str:
    sql = sql.strip()
    if sql.startswith("```"):
        lines = sql.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        sql = " ".join(lines)
    return re.sub(r" {2,}", " ", re.sub(r"[\r\n\t]+", " ", sql)).strip()


@dataclass(frozen=True)
class Text2SQLDependencies:
    retriever: Retriever
    generator: Generator
    validator: Validator
    executor: SqlExecutor
    repository: Repository


class Text2SQLService:
    def __init__(self, dependencies: Text2SQLDependencies, config: QueryServiceConfig):
        self._deps = dependencies
        self._config = config

    async def generate(
        self,
        question: str,
        *,
        max_retries: int = 2,
        execute_sql: bool = True,
        capture_feedback: bool = True,
    ) -> dict[str, Any]:
        last_sql: str | None = None
        last_error: str | None = None
        candidate_tables: list[str] = []
        candidate_scores: dict[str, float] = {}
        candidate_reasons: dict[str, Any] = {}
        refusal_reason: str | None = None

        try:
            context = await self._deps.retriever.retrieve(question)
        except Exception as exc:
            return build_query_response(
                success=False,
                question=question,
                attempts=1,
                error=f"上下文构建失败: {exc}",
            )
        prompt = str(context.get("prompt") or "")
        candidate_tables = context.get("candidate_tables", []) or []
        candidate_scores = context.get("candidate_scores", {}) or {}
        candidate_reasons = context.get("candidate_score_reasons", {}) or {}
        refusal_reason = str(context.get("insufficiency_reason") or "") or None
        if context.get("insufficient_context"):
            return build_query_response(
                success=False,
                question=question,
                attempts=1,
                error=refusal_reason or "问题无法映射到当前数据库结构。",
                candidate_tables=candidate_tables,
                candidate_scores=candidate_scores,
                candidate_score_reasons=candidate_reasons,
                refusal_reason=refusal_reason,
            )

        for attempt in range(max_retries + 1):
            try:
                generation_prompt = build_generation_prompt(
                    question, prompt, attempt, last_sql, last_error
                )
                response = await self._deps.generator.generate(generation_prompt)
                if not response:
                    last_error = "模型未返回有效 SQL"
                    continue
                sql = _normalize_sql_output(response)
                if sql == REFUSAL_TOKEN:
                    reason = refusal_reason or "模型判定当前问题缺少足够上下文。"
                    return build_query_response(
                        success=False,
                        question=question,
                        attempts=attempt + 1,
                        error=reason,
                        candidate_tables=candidate_tables,
                        candidate_scores=candidate_scores,
                        candidate_score_reasons=candidate_reasons,
                        refusal_reason=reason,
                    )

                last_sql = sql
                live_schema = context.get("live_schema")
                if live_schema:
                    validation_error = self._deps.validator.validate(
                        sql,
                        live_schema,
                        question=question,
                        semantic_ir=context.get("semantic_ir"),
                    )
                    if validation_error:
                        last_error = validation_error
                        continue

                if not execute_sql:
                    return build_query_response(
                        success=True,
                        question=question,
                        attempts=attempt + 1,
                        sql=sql,
                        candidate_tables=candidate_tables,
                        candidate_scores=candidate_scores,
                        candidate_score_reasons=candidate_reasons,
                    )

                execution_sql = limit_tsql_rows(sql, self._config.max_result_rows + 1)
                try:
                    result = await self._deps.executor.execute(
                        execution_sql,
                        timeout_seconds=self._config.query_timeout_seconds,
                    )
                except Exception as exc:
                    return build_query_response(
                        success=False,
                        question=question,
                        attempts=attempt + 1,
                        sql=sql,
                        error=f"SQL 执行失败: {exc}",
                        candidate_tables=candidate_tables,
                        candidate_scores=candidate_scores,
                        candidate_score_reasons=candidate_reasons,
                    )
                payload = result.to_dict(orient="records") if hasattr(result, "to_dict") else result
                total_rows = len(payload) if isinstance(payload, list) else 0
                if capture_feedback:
                    try:
                        await self._deps.repository.capture(
                            question,
                            sql,
                            candidate_tables,
                            execution_succeeded=True,
                            result_row_count=total_rows,
                            approved=total_rows >= self._config.feedback_min_result_rows,
                            capture_source="execution",
                        )
                    except Exception as exc:
                        logger.warning("反馈候选写入失败，不影响查询结果: %s", exc)
                truncated = isinstance(payload, list) and total_rows > self._config.max_result_rows
                if truncated:
                    payload = payload[: self._config.max_result_rows]
                return build_query_response(
                    success=True,
                    question=question,
                    attempts=attempt + 1,
                    sql=sql,
                    result=payload,
                    candidate_tables=candidate_tables,
                    candidate_scores=candidate_scores,
                    candidate_score_reasons=candidate_reasons,
                    result_truncated=truncated,
                    result_total_rows=total_rows,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Text2SQL attempt %d failed: %s", attempt + 1, last_error)

        return build_query_response(
            success=False,
            question=question,
            attempts=max_retries + 1,
            sql=last_sql,
            error=last_error or "生成 SQL 失败",
            candidate_tables=candidate_tables,
            candidate_scores=candidate_scores,
            candidate_score_reasons=candidate_reasons,
            refusal_reason=refusal_reason,
        )


async def generate_sql_with_feedback(
    question: str,
    *,
    service: Text2SQLService,
    max_retries: int = 2,
    execute_sql: bool = True,
    capture_feedback: bool = True,
) -> dict[str, Any]:
    """Execute the use case through an explicitly composed service."""
    return await service.generate(
        question,
        max_retries=max_retries,
        execute_sql=execute_sql,
        capture_feedback=capture_feedback,
    )
