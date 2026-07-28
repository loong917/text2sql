"""Orchestrate the online question-to-SQL use case.

The service obtains grounded context, calls Ollama, validates generated SQL,
optionally executes it through the read-only runner, captures pending feedback,
and returns a stable API payload. External boundaries are injectable through
``Text2SQLDependencies`` so the use case can be tested without live adapters.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import ollama
import re
from typing import Any, Awaitable, Callable, Optional

from vanna.capabilities.sql_runner.models import RunSqlToolArgs

from ..infrastructure.runtime import get_sql_runner
from ..core.config import settings
from ..core.logging import setup_logging
from ..infrastructure.feedback_repository import (
    capture_execution_feedback,
)
from .context_service import (
    REFUSAL_TOKEN,
    build_prompt_context,
    validate_sql,
)

logger = setup_logging("text2sql.application")

_ollama_client: Optional[ollama.AsyncClient] = None
_llm_semaphore = asyncio.Semaphore(settings.llm_max_concurrency)


def _get_ollama_client() -> ollama.AsyncClient:
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.AsyncClient(
            host=settings.llm_host,
            timeout=settings.llm_timeout_seconds,
        )
    return _ollama_client


def _decode_bytes(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.hex()


def _make_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _make_json_safe(item) for key, item in value.items()}

    if isinstance(value, list):
        return [_make_json_safe(item) for item in value]

    if isinstance(value, tuple):
        return [_make_json_safe(item) for item in value]

    if isinstance(value, (bytes, bytearray)):
        return _decode_bytes(bytes(value))

    if isinstance(value, memoryview):
        return _decode_bytes(value.tobytes())

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    return value


def _normalize_sql_output(sql: str) -> str:
    sql = sql.strip()
    if sql.startswith("```"):
        lines = sql.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        sql = " ".join(lines)
    sql = re.sub(r"[\r\n\t]+", " ", sql)
    sql = re.sub(r" {2,}", " ", sql)
    return sql.strip()

def _refusal_error(reason: str | None = None) -> str:
    return reason or "问题无法映射到当前数据库结构，已拒绝生成 SQL。"


def _build_response(
    *,
    success: bool,
    sql: Any,
    result: Any,
    attempts: int,
    error: Any,
    question: str,
    candidate_tables: list[str] | None = None,
    candidate_scores: dict[str, float] | None = None,
    candidate_score_reasons: dict[str, Any] | None = None,
    refusal_reason: str | None = None,
    result_truncated: bool = False,
    result_total_rows: int | None = None,
) -> dict[str, Any]:
    result_rows = result if isinstance(result, list) else None
    result_columns = list(result_rows[0].keys()) if result_rows else []
    returned_rows = len(result_rows) if result_rows is not None else 0
    return {
        "success": success,
        "question": question,
        "sql": _make_json_safe(sql),
        "result": _make_json_safe(result),
        "attempts": attempts,
        "error": _make_json_safe(error),
        "candidate_tables": _make_json_safe(candidate_tables or []),
        "candidate_scores": _make_json_safe(candidate_scores or {}),
        "candidate_score_reasons": _make_json_safe(candidate_score_reasons or {}),
        "refusal_reason": _make_json_safe(refusal_reason),
        "result_row_count": returned_rows,
        "result_total_rows": (
            result_total_rows if result_total_rows is not None else returned_rows
        ),
        "result_truncated": result_truncated,
        "result_columns": result_columns,
    }


async def _call_agent(message: str) -> str:
    """Call Ollama with an already-grounded prompt, bypassing a second RAG pass."""
    client = _get_ollama_client()
    async with _llm_semaphore:
        response = await client.chat(
            model=settings.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 SQL Server Text2SQL 生成器。严格执行给定语义 IR、实时 Schema "
                        "和业务规则；不得补造字段或实体值。只输出一条只读 T-SQL，无法确定时输出 "
                        f"{REFUSAL_TOKEN}。"
                    ),
                },
                {"role": "user", "content": message},
            ],
            options={
                "temperature": 0,
                "num_ctx": settings.llm_num_ctx,
                "num_predict": settings.llm_num_predict,
            },
            keep_alive=settings.llm_keep_alive,
        )
    return response.get("message", {}).get("content", "")


@dataclass(frozen=True)
class Text2SQLDependencies:
    """Injectable boundaries used by the online Text2SQL use case."""

    sql_runner_provider: Callable[[], Any]
    context_builder: Callable[[str], Awaitable[dict[str, Any]]]
    sql_validator: Callable[..., str | None]
    llm_generator: Callable[[str], Awaitable[str]]
    feedback_capture: Callable[..., Awaitable[Any]]


DEFAULT_DEPENDENCIES = Text2SQLDependencies(
    sql_runner_provider=get_sql_runner,
    context_builder=build_prompt_context,
    sql_validator=validate_sql,
    llm_generator=_call_agent,
    feedback_capture=capture_execution_feedback,
)



async def generate_sql_with_feedback(
    question: str,
    max_retries: int = 2,
    execute_sql: bool = True,
    capture_feedback: bool = True,
    dependencies: Text2SQLDependencies | None = None,
) -> dict[str, Any]:
    """Generate, validate, optionally execute, and report one Text2SQL request.

    ``max_retries`` counts corrective generations after the first attempt.
    Setting ``execute_sql`` to false still performs grounding and validation.
    Automatic feedback capture creates Pending records only.
    """
    deps = dependencies or DEFAULT_DEPENDENCIES
    sql_runner = deps.sql_runner_provider()

    last_sql = None
    last_error = None
    last_candidate_tables: list[str] = []
    last_candidate_scores: dict[str, float] = {}
    last_candidate_score_reasons: dict[str, Any] = {}
    last_refusal_reason: str | None = None

    for attempt in range(max_retries + 1):
        try:
            prompt_context = await deps.context_builder(question)
            prompt_prefix = prompt_context.get("prompt", "")
            insufficient_context = bool(prompt_context.get("insufficient_context"))
            insufficiency_reason = prompt_context.get("insufficiency_reason", "")
            last_candidate_tables = prompt_context.get("candidate_tables", []) or []
            last_candidate_scores = prompt_context.get("candidate_scores", {}) or {}
            last_candidate_score_reasons = (
                prompt_context.get("candidate_score_reasons", {}) or {}
            )
            last_refusal_reason = insufficiency_reason or None

            if insufficient_context:
                refusal_message = _refusal_error(insufficiency_reason)
                logger.warning(
                    "Refused to generate SQL due to insufficient context: %s",
                    refusal_message,
                )
                return _build_response(
                    success=False,
                    sql=None,
                    result=None,
                    attempts=attempt + 1,
                    error=refusal_message,
                    question=question,
                    candidate_tables=last_candidate_tables,
                    candidate_scores=last_candidate_scores,
                    candidate_score_reasons=last_candidate_score_reasons,
                    refusal_reason=insufficiency_reason,
                )

            if attempt > 0 and last_error:
                previous_sql_line = (
                    f"上一次生成的 SQL：{last_sql}\n" if last_sql else ""
                )
                current_question = (f"{prompt_prefix}\n\n" if prompt_prefix else "") + (
                    f"上一次为问题 '{question}' 生成的 SQL 执行失败了。\n"
                    f"{previous_sql_line}"
                    f"错误信息：{last_error}\n"
                    f"请根据错误信息修正并重新生成正确的 SQL Server 查询语句。\n"
                    f"如果仍然无法从真实表结构中确定表名、字段名或关联关系，请直接返回 {REFUSAL_TOKEN}。\n"
                    f"【重要】请只输出单行 SQL 语句本身，不要输出任何 JSON、不要输出任何 Markdown 格式（如 ```sql）、不要包含任何解释性文字，且结果中不要包含换行符。"
                )
                logger.info(f"Retry attempt {attempt + 1} with error feedback")
            else:
                current_question = (
                    f"{prompt_prefix}\n\n【输出要求】\n请只返回单行 SQL 语句，不要返回 JSON，不要返回 Markdown，不要包含解释，不要包含换行符；如果上下文不足以确定真实表结构，请直接返回 {REFUSAL_TOKEN}。\n\n【用户问题】\n{question}"
                    if prompt_prefix
                    else f"请只返回单行 SQL 语句，不要返回 JSON，不要返回 Markdown，不要包含解释，不要包含换行符；如果上下文不足以确定真实表结构，请直接返回 {REFUSAL_TOKEN}。\n\n{question}"
                )

            logger.info(f"Calling Ollama directly (attempt {attempt + 1})...")
            response = await deps.llm_generator(current_question)

            if not response:
                last_error = "Agent 未返回有效的 SQL"
                continue

            sql = _normalize_sql_output(response)

            if sql == REFUSAL_TOKEN:
                refusal_message = _refusal_error(
                    insufficiency_reason or "模型判定当前问题缺少足够上下文。"
                )
                logger.warning("Model refused to generate SQL: %s", refusal_message)
                return _build_response(
                    success=False,
                    sql=None,
                    result=None,
                    attempts=attempt + 1,
                    error=refusal_message,
                    question=question,
                    candidate_tables=last_candidate_tables,
                    candidate_scores=last_candidate_scores,
                    candidate_score_reasons=last_candidate_score_reasons,
                    refusal_reason=insufficiency_reason
                    or "模型判定当前问题缺少足够上下文。",
                )

            last_sql = sql
            logger.info(f"Generated SQL (attempt {attempt + 1}):\n{last_sql}")

            live_schema = (prompt_context or {}).get("live_schema")
            if live_schema:
                validation_error = deps.sql_validator(
                    last_sql,
                    live_schema,
                    question=question,
                    semantic_ir=(prompt_context or {}).get("semantic_ir"),
                )
                if validation_error:
                    last_error = validation_error
                    logger.warning(
                        "Generated SQL failed local schema validation on attempt %d: %s",
                        attempt + 1,
                        validation_error,
                    )
                    if attempt == max_retries:
                        break
                    continue

            if not execute_sql:
                return _build_response(
                    success=True,
                    sql=last_sql,
                    result=None,
                    attempts=attempt + 1,
                    error=None,
                    question=question,
                    candidate_tables=last_candidate_tables,
                    candidate_scores=last_candidate_scores,
                    candidate_score_reasons=last_candidate_score_reasons,
                    refusal_reason=None,
                )

            try:
                args = RunSqlToolArgs(sql=last_sql)
                result_df = await sql_runner.run_sql(args, None)

                logger.info(f"SQL executed successfully on attempt {attempt + 1}")
                result_payload = (
                    result_df.to_dict(orient="records")
                    if hasattr(result_df, "to_dict")
                    else result_df
                )
                result_row_count = len(result_payload) if isinstance(result_payload, list) else 0
                if capture_feedback:
                    await deps.feedback_capture(
                        question,
                        last_sql,
                        last_candidate_tables,
                        execution_succeeded=True,
                        result_row_count=result_row_count,
                        approved=result_row_count >= settings.feedback_min_result_rows,
                        capture_source="execution",
                    )

                result_truncated = False
                if (
                    isinstance(result_payload, list)
                    and result_row_count > settings.max_result_rows
                ):
                    result_payload = result_payload[: settings.max_result_rows]
                    result_truncated = True
                    logger.info(
                        "Result truncated from %d to %d rows",
                        result_row_count,
                        settings.max_result_rows,
                    )

                return _build_response(
                    success=True,
                    sql=last_sql,
                    result=result_payload,
                    attempts=attempt + 1,
                    error=None,
                    question=question,
                    candidate_tables=last_candidate_tables,
                    candidate_scores=last_candidate_scores,
                    candidate_score_reasons=last_candidate_score_reasons,
                    refusal_reason=None,
                    result_truncated=result_truncated,
                    result_total_rows=result_row_count,
                )

            except Exception as exec_error:
                last_error = str(exec_error)
                logger.warning(
                    f"SQL execution failed on attempt {attempt + 1}: {last_error}"
                )

                if attempt == max_retries:
                    break

        except Exception as e:
            last_error = str(e)
            logger.error(f"Agent call failed on attempt {attempt + 1}: {last_error}")
            if attempt == max_retries:
                break

    return _build_response(
        success=False,
        sql=last_sql,
        result=None,
        attempts=max_retries + 1,
        error=last_error or "生成 SQL 失败",
        question=question,
        candidate_tables=last_candidate_tables,
        candidate_scores=last_candidate_scores,
        candidate_score_reasons=last_candidate_score_reasons,
        refusal_reason=last_refusal_reason,
    )
