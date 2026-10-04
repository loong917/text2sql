"""Generate and validate SQL over typed application contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..core.logging import setup_logging
from .constants import REFUSAL_TOKEN
from .contracts import QueryContext, QueryOutcome, ValidationResult
from .ports import Generator, Repository, Retriever, SqlCompiler, SqlExecutor, Validator
from .query_config import QueryServiceConfig
from .query_execution import execute_query
from .query_prompt import build_generation_prompt
from .query_response import build_query_response

logger = setup_logging("text2sql.application")


def _normalize_sql_output(sql: str) -> str:
    """Remove a surrounding Markdown fence without changing SQL semantics."""
    sql = sql.strip()
    if sql.startswith("```"):
        lines = sql.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        sql = "\n".join(lines)
    return sql.strip()


@dataclass(frozen=True)
class Text2SQLDependencies:
    retriever: Retriever
    generator: Generator
    validator: Validator
    executor: SqlExecutor
    repository: Repository
    compiler: SqlCompiler | None = None


class Text2SQLService:
    def __init__(self, dependencies: Text2SQLDependencies, config: QueryServiceConfig):
        self._deps = dependencies
        self._config = config

    async def aclose(self) -> None:
        """Attempt all owned closes, aggregate errors, and preserve cancellation."""
        failures: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        for resource in (self._deps.generator, self._deps.retriever):
            try:
                close = getattr(resource, "aclose", None)
                if close is not None:
                    await close()
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception as exc:
                exc.add_note(f"while closing {type(resource).__name__}")
                failures.append(exc)
        if cancelled is not None:
            if failures:
                raise cancelled from ExceptionGroup("Text2SQLService shutdown failed", failures)
            raise cancelled
        if failures:
            raise ExceptionGroup("Text2SQLService shutdown failed", failures)

    async def generate(
        self,
        question: str,
        *,
        max_retries: int = 2,
        execute_sql: bool = True,
        capture_feedback: bool = True,
        preserve_result_types: bool = False,
    ) -> dict[str, Any]:
        try:
            context = await self._deps.retriever.retrieve(question)
            if not isinstance(context, QueryContext):
                raise TypeError("Retriever must return QueryContext")
        except Exception as exc:
            logger.warning("Context construction failed: %s", type(exc).__name__)
            return build_query_response(
                success=False,
                question=question,
                attempts=1,
                outcome="infrastructure_error",
                error_code="CONTEXT_UNAVAILABLE",
                error="上下文构建失败，请检查知识产物和数据库",
            )
        common: dict[str, Any] = {
            "question": question,
            "candidate_tables": context.candidate_tables,
            "candidate_scores": context.candidate_scores,
            "candidate_score_reasons": context.candidate_score_reasons,
            "diagnostics": context.diagnostics,
        }
        if context.insufficient_context:
            reason = context.insufficiency_reason or "问题无法映射到当前业务目录。"
            return build_query_response(
                success=False,
                attempts=1,
                outcome=context.outcome,
                error_code="CLARIFICATION_REQUIRED"
                if context.outcome == "clarification_required"
                else "OUT_OF_SCOPE",
                error=reason,
                refusal_reason=reason,
                **common,
            )
        if not context.live_schema:
            return build_query_response(
                success=False,
                attempts=1,
                outcome="infrastructure_error",
                error_code="SCHEMA_UNAVAILABLE",
                error="缺少权威 Schema，禁止生成和执行 SQL",
                **common,
            )
        last_sql = last_error = None
        failure_outcome: QueryOutcome = "generation_failed"
        failure_code = "EMPTY_GENERATION"
        attempts = max(0, min(max_retries, 3)) + 1
        compiled = None
        if self._deps.compiler and context.semantic_ir is not None:
            try:
                compiled = self._deps.compiler.compile(context.semantic_ir)
                if compiled is not None and not isinstance(compiled, str):
                    raise TypeError("Compiler must return str or None")
            except Exception as exc:
                logger.warning("Plan compilation failed: %s", type(exc).__name__)
                return build_query_response(
                    success=False,
                    attempts=1,
                    outcome="validation_failed",
                    error_code="PLAN_COMPILATION_FAILED",
                    error="语义计划无法安全编译",
                    **common,
                )
        for attempt in range(attempts):
            try:
                raw = (
                    compiled
                    if compiled
                    else await self._deps.generator.generate(
                        build_generation_prompt(
                            question, context.prompt, attempt, last_sql, last_error
                        )
                    )
                )
                if not isinstance(raw, str):
                    raise ValueError("Generator must return a SQL string")
            except ValueError as exc:
                logger.warning("Invalid model output: %s", type(exc).__name__)
                last_error = "模型输出格式不符合约定"
                failure_outcome = "generation_failed"
                failure_code = "MODEL_OUTPUT_INVALID"
                continue
            except Exception as exc:
                logger.warning("Generation failed: %s", type(exc).__name__)
                return build_query_response(
                    success=False,
                    attempts=attempt + 1,
                    outcome="infrastructure_error",
                    error_code="MODEL_UNAVAILABLE",
                    error="模型服务暂时不可用",
                    **common,
                )
            sql = _normalize_sql_output(raw)
            if sql == REFUSAL_TOKEN:
                reason = "当前问题缺少足够业务依据，不能可靠生成 SQL。"
                return build_query_response(
                    success=False,
                    attempts=attempt + 1,
                    outcome="refused",
                    error_code="MODEL_REFUSAL",
                    error=reason,
                    refusal_reason=reason,
                    **common,
                )
            last_sql = sql
            if not sql:
                last_error = "模型未返回有效 SQL"
                failure_outcome = "generation_failed"
                failure_code = "EMPTY_GENERATION"
                continue
            try:
                validation = self._deps.validator.validate(
                    sql,
                    context.live_schema,
                    question=question,
                    semantic_ir=context.semantic_ir,
                )
                if not isinstance(validation, ValidationResult):
                    raise TypeError("Validator must return ValidationResult")
            except Exception as exc:
                logger.warning("Validation service failed: %s", type(exc).__name__)
                return build_query_response(
                    success=False,
                    attempts=attempt + 1,
                    outcome="infrastructure_error",
                    error_code="VALIDATOR_UNAVAILABLE",
                    error="SQL 校验服务暂时不可用",
                    **common,
                )
            if not validation.valid:
                last_error = validation.message or "SQL 校验失败"
                failure_outcome = "validation_failed"
                if compiled:
                    break
                continue
            if not execute_sql:
                return build_query_response(success=True, attempts=attempt + 1, sql=sql, **common)
            return await execute_query(
                question,
                sql,
                context,
                executor=self._deps.executor,
                repository=self._deps.repository,
                config=self._config,
                attempts=attempt + 1,
                capture_feedback=capture_feedback,
                preserve_result_types=preserve_result_types,
            )
        return build_query_response(
            success=False,
            attempts=attempt + 1,
            sql=last_sql,
            error=last_error or "生成 SQL 失败",
            outcome=failure_outcome,
            error_code="SQL_VALIDATION_FAILED"
            if failure_outcome == "validation_failed"
            else failure_code,
            **common,
        )
