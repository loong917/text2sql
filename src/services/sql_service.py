"""
整体职责
1）负责把用户问题变成 SQL，并在需要时执行 SQL、记录反馈、支持重试修正。

核心主链路
1）接收自然语言问题。
2）调用 build_prompt_context() 获取候选表、schema 约束、训练规则和拒答判断。
3）组装 prompt，直调 Ollama 生成 SQL。
4）清洗模型输出并校验。
5）调用 validate_sql() 做本地结构与业务规则校验。
6）可选执行 SQL，并把成功样本写入反馈库。
7）返回标准化响应给 /ask 或 /generate-sql 接口。

定位
- 相对 schema_service.py ：它不负责“理解真实结构”，而是消费结构约束结果来驱动生成和校验。
- 相对 training.py ：它不负责“写入知识”，而是消费训练后的知识索引和反馈样本。
- 所以它是连接“用户问题 -> prompt -> LLM -> SQL -> 执行/反馈”的运行时总编排器。

一句话总结
- schema_service.py 决定“模型应该怎么被约束”；
- sql_service.py 决定“模型怎么生成、怎么校验、怎么执行、怎么回流学习”。

"""

import asyncio
from datetime import date, datetime, time
from decimal import Decimal
import ollama
import re
from typing import Any, Optional

from vanna.capabilities.sql_runner.models import RunSqlToolArgs

from ..core.agent import get_sql_runner
from ..core.config import settings
from ..core.logging import setup_logging
from .feedback_store import capture_execution_feedback, submit_online_feedback
from .schema_service import (
    REFUSAL_TOKEN,
    build_prompt_context,
    validate_sql,
)

logger = setup_logging("text2sql.sql_service")

# 复用同一个异步客户端，避免每次请求重建连接池
_ollama_client: Optional[ollama.AsyncClient] = None
# 并发闸门：本地 Ollama 吞吐有限，超过并发上限的请求排队等待而不是压垮模型
_llm_semaphore = asyncio.Semaphore(settings.llm_max_concurrency)


def _get_ollama_client() -> ollama.AsyncClient:
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.AsyncClient(
            host=settings.llm_host,
            timeout=settings.llm_timeout_seconds,
        )
    return _ollama_client


# 把二进制结果转成可序列化字符串；优先按 UTF-8 解码，失败时退回十六进制，避免结果集里有二进制字段时 JSON 序列化报错。
def _decode_bytes(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # 二进制列不一定是 UTF-8 文本，无法可靠解码时退回十六进制字符串。
        return value.hex()


# 递归把返回结果转换成 JSON 安全类型。
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


# 把 SQL 压成单行、去掉多余空白，保证输出稳定，便于比较、存储和前端展示。
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

# 统一生成拒答错误文案，用于上下文不足或模型主动拒答时返回给前端。
def _refusal_error(reason: str | None = None) -> str:
    return reason or "问题无法映射到当前数据库结构，已拒绝生成 SQL。"


# 把成功/失败、SQL、结果集、尝试次数、错误信息、候选表、命中原因等统一包装成标准响应结构，是 /ask 和 /generate-sql 共用的输出格式基础。
def _build_response(
    *,
    success: bool,
    sql: Any,
    result: Any,
    attempts: int,
    error: Any,
    question: str,
    candidate_tables: list[str] | None = None,
    candidate_scores: dict[str, int] | None = None,
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


# 把拼好的最终 prompt 发给 LLM，并取回文本响应。
async def _call_agent(message: str) -> str:
    # 直接调用 Ollama 接口生成文本，绕过 Vanna 复杂的组件组装和历史记忆
    # 避免双重 RAG（在 generate_sql_with_feedback 已经构建好了包含 prompt_context 的 message）
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


# 整个文件最重要的方法，也是在线生成主流程总控。

# 主要职责：
# - 初始化 SQL runner 和一些状态变量，如上一次错误、候选表、拒答原因。
# - 每轮尝试先调用 build_prompt_context(question) 获取 schema 约束、候选表、记忆规则和拒答信息。
# - 如果上下文不足，直接拒答返回，不让模型发散生成。
# - 若是重试场景，则把上一轮错误拼进 prompt，让模型基于错误继续修正 SQL。
# - 调用 _call_agent() 生成 SQL，并兼容多种模型输出格式。
# - 若模型返回拒答 token，则按拒答路径返回。
# - 拿到 SQL 后调用 validate_sql(last_sql, live_schema, question=question) 做本地校验，拦截未知表字段和缺少 BCType 之类业务硬约束的 SQL。
# - 若 execute_sql=False ，则直接返回 SQL，不执行数据库查询，这也是 /generate-sql 接口使用的模式。
# - 若需要执行 SQL，则通过 sql_runner.run_sql() 执行，并在成功后持久化反馈样本。
# - 若执行失败，则记录错误进入下一轮重试；最终所有重试失败时返回失败响应。
async def generate_sql_with_feedback(
    question: str,
    max_retries: int = 2,
    execute_sql: bool = True,
    capture_feedback: bool = True,
) -> dict[str, Any]:
    """
    带执行反馈的 Text2SQL 生成

    Args:
        question: 用户自然语言问题
        max_retries: 最大重试次数（默认2次，即最多尝试3次）
        execute_sql: 是否执行 SQL（调试时可设为 False）

    Returns:
        {
            "success": bool,
            "sql": str,
            "result": Any,           # 执行结果（DataFrame）
            "attempts": int,
            "error": Optional[str]
        }
    """
    sql_runner = get_sql_runner()

    last_sql = None
    last_error = None
    last_candidate_tables: list[str] = []
    last_candidate_scores: dict[str, int] = {}
    last_candidate_score_reasons: dict[str, Any] = {}
    last_refusal_reason: str | None = None

    for attempt in range(max_retries + 1):
        try:
            prompt_context = await build_prompt_context(question)
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

            # 构造带错误反馈的问题
            if attempt > 0 and last_error:
                # 带上上一次的 SQL 和错误信息，让模型有明确的修正目标
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
            response = await _call_agent(current_question)

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
                validation_error = validate_sql(
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

            # 执行 SQL
            try:
                args = RunSqlToolArgs(sql=last_sql)
                ctx = None  # 可根据需要传入 ToolContext
                result_df = await sql_runner.run_sql(args, ctx)

                logger.info(f"SQL executed successfully on attempt {attempt + 1}")
                result_payload = (
                    result_df.to_dict(orient="records")
                    if hasattr(result_df, "to_dict")
                    else result_df
                )
                result_row_count = len(result_payload) if isinstance(result_payload, list) else 0
                if capture_feedback:
                    await capture_execution_feedback(
                        question,
                        last_sql,
                        last_candidate_tables,
                        execution_succeeded=True,
                        result_row_count=result_row_count,
                        approved=result_row_count >= settings.feedback_min_result_rows,
                        capture_source="execution",
                    )

                # 结果集截断：避免超大结果集拖垮 JSON 序列化和前端渲染
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

    # 所有重试都失败
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
