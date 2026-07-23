import asyncio
import json
import time
import signal
import threading
import traceback
from contextlib import asynccontextmanager, contextmanager, suppress
from functools import lru_cache
from pathlib import Path
from types import MethodType

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..services.sql_service import (
    generate_sql_with_feedback,
    submit_online_feedback,
)

from ..core.agent import get_runtime_status, initialize_runtime, reset_runtime
from ..core.config import settings
from ..core.logging import setup_logging

logger = setup_logging("text2sql.server")


TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "index.html"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@lru_cache(maxsize=1)
def _render_index_html() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _read_json_file(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _build_training_report_summary(report: dict, manifest: dict | None) -> dict:
    evaluation_summary = report.get("evaluation_summary") or {}
    total = int(evaluation_summary.get("total") or 0)
    passed = int(evaluation_summary.get("passed") or 0)
    failed = int(evaluation_summary.get("failed") or 0)
    pass_rate = round(passed * 100 / total, 1) if total else None
    table_names = []
    baseline_failed_cases = []
    if isinstance(manifest, dict):
        table_names = [str(item) for item in manifest.get("table_names", [])[:8]]
    for item in report.get("evaluations", []) or []:
        checks = item.get("checks", []) or []
        failed_check_names = [
            str(check.get("name") or "")
            for check in checks
            if not bool(check.get("passed"))
        ]
        if not (
            item.get("baseline_error")
            or "baseline_execution_success" in failed_check_names
            or "baseline_result_columns" in failed_check_names
            or "baseline_result_row_count" in failed_check_names
            or "baseline_result_match" in failed_check_names
        ):
            continue
        baseline_failed_cases.append(
            {
                "case_index": item.get("case_index"),
                "question": item.get("question"),
                "actual_sql": item.get("actual_sql"),
                "error": item.get("error"),
                "baseline_error": item.get("baseline_error"),
                "result_row_count": item.get("result_row_count", 0),
                "failed_checks": failed_check_names,
            }
        )

    return {
        "finished_at": report.get("finished_at"),
        "include_samples": bool(report.get("include_samples")),
        "sample_rows": report.get("sample_rows"),
        "table_count": report.get("table_count", 0),
        "column_count": report.get("column_count", 0),
        "knowledge_records": report.get("knowledge_records", 0),
        "feedback_examples": report.get("feedback_examples", 0),
        "question_sql_examples": report.get("question_sql_examples", 0),
        "warnings_count": len(report.get("warnings", []) or []),
        "evaluation_total": total,
        "evaluation_passed": passed,
        "evaluation_failed": failed,
        "evaluation_pass_rate": pass_rate,
        "table_names_preview": table_names,
        "baseline_failed_count": len(baseline_failed_cases),
        "baseline_failed_cases": baseline_failed_cases[:5],
    }


# ===== 请求模型：接口参数校验（非法请求由 FastAPI 直接返回 422） =====


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    max_retries: int = Field(default=2, ge=0, le=3)
    execute_sql: bool = True


class GenerateSqlRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class FeedbackValidationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    sql: str = Field(min_length=1, max_length=8000)
    validation_label: str
    candidate_tables: list[str] = Field(default_factory=list)
    candidate_score_reasons: dict = Field(default_factory=dict)
    comment: str = ""
    result_row_count: int = Field(default=0, ge=0)
    had_execution_result: bool = False


# 可选 API Key 鉴权：仅当配置了 API_KEY 时启用，静态页面和健康检查放行
class ApiKeyMiddleware(BaseHTTPMiddleware):
    PROTECTED_PREFIXES = ("/ask", "/generate-sql", "/feedback-validation", "/training-report")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if settings.api_key and path.startswith(self.PROTECTED_PREFIXES):
            if request.headers.get("x-api-key") != settings.api_key:
                return JSONResponse(
                    {"success": False, "error": "无效或缺失的 API Key"},
                    status_code=401,
                )
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response


# uvicorn 退出后重抛信号导致的 KeyboardInterrupt
class QuietUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Uvicorn 0.48 re-raises captured SIGINT/SIGTERM after shutdown.
        # On Python 3.14, repeated Ctrl+C can then surface as an ERROR trace
        # even though shutdown has already been handled gracefully.
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        original_handlers = {
            sig: signal.signal(sig, self.handle_exit)
            for sig in uvicorn.server.HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)


# starlette 关闭时 receive() 被取消导致的 CancelledError
async def _quiet_router_lifespan(self, scope, receive, send) -> None:
    started = False
    app = scope.get("app")
    await receive()
    try:
        async with self.lifespan_context(app) as maybe_state:
            if maybe_state is not None:
                if "state" not in scope:
                    raise RuntimeError(
                        'The server does not support "state" in the lifespan scope.'
                    )
                scope["state"].update(maybe_state)
            await send({"type": "lifespan.startup.complete"})
            started = True
            await receive()
    except asyncio.CancelledError:
        if started:
            logger.info("Lifespan receive cancelled during forced shutdown")
            return
        raise
    except BaseException:
        exc_text = traceback.format_exc()
        if started:
            await send({"type": "lifespan.shutdown.failed", "message": exc_text})
        else:
            await send({"type": "lifespan.startup.failed", "message": exc_text})
        raise
    else:
        await send({"type": "lifespan.shutdown.complete"})


def run_server() -> None:
    initialize_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            logger.info("Running shutdown cleanup")
            try:
                reset_runtime()
            except Exception:
                logger.exception("Error during shutdown cleanup")
            else:
                logger.info("Shutdown cleanup complete")

    app = FastAPI(title="Text2SQL API", version="1.0.0", lifespan=lifespan)
    app.router.lifespan = MethodType(_quiet_router_lifespan, app.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_middleware(RequestLoggingMiddleware)
    if settings.api_key:
        app.add_middleware(ApiKeyMiddleware)
        logger.info("API Key 鉴权已启用")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_render_index_html())

    @app.get("/health")
    async def health():
        try:
            runtime = get_runtime_status()
        except Exception as exc:
            logger.warning("健康检查失败: %s", exc)
            return JSONResponse(
                {"status": "unhealthy", "error": str(exc)}, status_code=503
            )
        return {"status": "healthy", "runtime": runtime}

    @app.get("/training-report")
    async def training_report():
        try:
            report = _read_json_file(Path(settings.training_report_path))
            manifest = _read_json_file(Path(settings.training_manifest_path))
        except Exception as exc:
            logger.warning("读取训练报告失败: %s", exc)
            return {
                "success": False,
                "available": False,
                "error": f"读取训练报告失败: {exc}",
                "summary": None,
                "report": None,
                "manifest": None,
            }

        if not isinstance(report, dict):
            return {
                "success": True,
                "available": False,
                "error": None,
                "summary": None,
                "report": None,
                "manifest": manifest if isinstance(manifest, dict) else None,
            }

        manifest_payload = manifest if isinstance(manifest, dict) else None
        return {
            "success": True,
            "available": True,
            "error": None,
            "summary": _build_training_report_summary(report, manifest_payload),
            "report": report,
            "manifest": manifest_payload,
        }

    # 在创建 app 后添加新路由
    @app.post("/ask")
    async def ask_with_feedback_endpoint(payload: AskRequest):
        """
        带执行反馈的 Text2SQL 接口
        """
        result = await generate_sql_with_feedback(
            question=payload.question.strip(),
            max_retries=payload.max_retries,
            execute_sql=payload.execute_sql,
        )

        # 只记录摘要，不把整份结果集打进日志
        logger.info(
            "/ask 完成: success=%s attempts=%s rows=%s truncated=%s sql=%s",
            result.get("success"),
            result.get("attempts"),
            result.get("result_total_rows"),
            result.get("result_truncated"),
            str(result.get("sql") or "")[:300],
        )

        return result

    @app.post("/generate-sql")
    async def generate_sql_only(payload: GenerateSqlRequest):
        """仅生成 SQL，不执行（返回纯文本单行 SQL）"""
        result = await generate_sql_with_feedback(
            payload.question.strip(), max_retries=1, execute_sql=False
        )
        if not result.get("success"):
            return PlainTextResponse(
                result.get("error") or "生成 SQL 失败",
                status_code=400,
            )

        return PlainTextResponse(result.get("sql", ""))

    @app.post("/feedback-validation")
    async def feedback_validation(payload: FeedbackValidationRequest):
        if payload.validation_label not in {"correct", "incorrect"}:
            return JSONResponse(
                {
                    "success": False,
                    "error": "validation_label 必须为 correct 或 incorrect",
                },
                status_code=400,
            )

        try:
            # 反馈落盘涉及文件读写，放到线程池避免阻塞事件循环
            result = await asyncio.to_thread(
                submit_online_feedback,
                question=payload.question.strip(),
                sql=payload.sql.strip(),
                candidate_tables=[str(item) for item in payload.candidate_tables],
                candidate_score_reasons=payload.candidate_score_reasons,
                validation_label=payload.validation_label,
                comment=payload.comment.strip(),
                result_row_count=payload.result_row_count,
                had_execution_result=payload.had_execution_result,
            )
        except Exception as exc:
            logger.warning("在线反馈提交失败: %s", exc)
            return JSONResponse(
                {"success": False, "error": f"在线反馈提交失败: {exc}"},
                status_code=500,
            )

        return result

    config = uvicorn.Config(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
        timeout_keep_alive=settings.timeout_keep_alive,
    )

    logger.info(
        "Starting uvicorn server at %s:%d",
        settings.server_host,
        settings.server_port,
    )

    server = QuietUvicornServer(config)
    with suppress(KeyboardInterrupt):
        server.run()
