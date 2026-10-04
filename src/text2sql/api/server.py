"""FastAPI delivery layer and Uvicorn process entry point."""

import asyncio
import hashlib
import re
import signal
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..application.feedback_service import review_feedback
from ..bootstrap import ApplicationContainer
from ..core.config import Settings, load_settings
from ..core.exceptions import ConfigurationError
from ..core.logging import setup_logging
from ..core.production import production_configuration_errors
from ..infrastructure.database_preflight import (
    database_configuration_errors,
    run_database_preflight,
)
from ..release.runtime_gate import ProductionReleaseGate
from .auth import ProductionAuthMiddleware, SessionCodec, install_auth_routes
from .errors import install_exception_handlers
from .health import build_readiness
from .schemas import (
    AskRequest,
    FeedbackResponse,
    FeedbackValidationRequest,
    GenerateSqlRequest,
    QueryResponse,
    ReadinessResponse,
    TrainingReportResponse,
)
from .training_report import load_active_training_report

logger = setup_logging("text2sql.server")


TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "index.html"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _application_version() -> str:
    try:
        return version("text2sql")
    except PackageNotFoundError:
        return "development"


@lru_cache(maxsize=1)
def _render_index_html() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a safe correlation ID and log status plus wall-clock latency."""

    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_id=%s %s %s -> %d (%.1fms)",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response


class QuietUvicornServer(uvicorn.Server):
    """Preserve signal handling while tolerating embedded server threads."""

    @contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        original_handlers = {
            sig: signal.signal(sig, self.handle_exit) for sig in uvicorn.server.HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)


def create_app(
    config: Settings | None = None,
    container: ApplicationContainer | None = None,
) -> FastAPI:
    """Build the HTTP application without starting a server process."""
    config = config or load_settings()
    configuration_errors = [
        *production_configuration_errors(config),
        *database_configuration_errors(config),
    ]
    if config.app_env == "production" and configuration_errors:
        raise ConfigurationError("生产配置未通过安全检查: " + "; ".join(configuration_errors))
    container = container or ApplicationContainer(config)
    release_gate = ProductionReleaseGate(config)
    release_gate.validate()
    session_codec = SessionCodec(config.web_session_secret, config.web_session_ttl_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if config.app_env == "production":
                await asyncio.to_thread(release_gate.validate)
                preflight = await asyncio.to_thread(run_database_preflight, config)
                if not preflight.success:
                    raise ConfigurationError("生产数据库 TLS/只读权限预检失败")
            yield
        finally:
            logger.info("Running shutdown cleanup")
            try:
                if hasattr(container, "aclose"):
                    await container.aclose()
                else:
                    container.close()
            except Exception:
                logger.exception("Error during shutdown cleanup")
            else:
                logger.info("Shutdown cleanup complete")

    app = FastAPI(title="Text2SQL API", version=_application_version(), lifespan=lifespan)
    install_exception_handlers(app)
    app.state.container = container
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_middleware(
        ProductionAuthMiddleware,
        config=config,
        session_codec=session_codec,
    )
    # Added last so rejected authentication requests are also correlated and logged.
    app.add_middleware(RequestContextMiddleware)
    if config.api_key or config.feedback_admin_api_key:
        logger.info("API Key 鉴权已启用")

    install_auth_routes(app, config, session_codec)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_render_index_html())

    @app.get("/livez")
    async def liveness():
        return {"status": "alive"}

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readiness():
        try:
            await asyncio.to_thread(release_gate.validate)
        except ConfigurationError:
            return JSONResponse(
                {"status": "not_ready", "checks": {"release": "invalid"}}, status_code=503
            )
        payload, status_code = await build_readiness(config, container)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/training-report", response_model=TrainingReportResponse)
    async def training_report():
        try:
            return await asyncio.to_thread(load_active_training_report, config)
        except Exception as exc:
            logger.warning("读取训练报告失败: %s", exc)
            return {
                "success": False,
                "available": False,
                "error": "训练报告暂时不可用，请查看服务端日志",
                "summary": None,
                "report": None,
                "manifest": None,
            }

    @app.post("/ask", response_model=QueryResponse)
    async def ask_with_feedback_endpoint(payload: AskRequest):
        """
        带执行反馈的 Text2SQL 接口
        """
        await asyncio.to_thread(release_gate.validate)
        service = await asyncio.to_thread(getattr, container, "query_service")
        result = await service.generate(
            payload.question.strip(),
            max_retries=payload.max_retries,
            execute_sql=payload.execute_sql,
        )

        sql_fingerprint = hashlib.sha256(str(result.get("sql") or "").encode("utf-8")).hexdigest()[
            :12
        ]
        logger.info(
            "/ask 完成: success=%s attempts=%s rows=%s truncated=%s sql_hash=%s",
            result.get("success"),
            result.get("attempts"),
            result.get("result_total_rows"),
            result.get("result_truncated"),
            sql_fingerprint,
        )
        if not result.get("success") and not result.get("refusal_reason"):
            result = {**result, "error": "SQL 生成、校验或执行失败"}
        return result

    @app.post("/generate-sql")
    async def generate_sql_only(payload: GenerateSqlRequest):
        """仅生成 SQL，不执行；保留 SQL 中的字面量和注释换行。"""
        await asyncio.to_thread(release_gate.validate)
        service = await asyncio.to_thread(getattr, container, "query_service")
        result = await service.generate(
            payload.question.strip(),
            max_retries=1,
            execute_sql=False,
        )
        if not result.get("success"):
            return PlainTextResponse(
                "SQL 生成或校验失败",
                status_code=400,
            )

        return PlainTextResponse(result.get("sql", ""))

    @app.post("/feedback", response_model=FeedbackResponse)
    async def feedback_candidate(payload: FeedbackValidationRequest):
        try:
            repository = await asyncio.to_thread(getattr, container, "feedback_repository")
            return await asyncio.to_thread(
                repository.submit_candidate_review,
                question=payload.question.strip(),
                sql=payload.sql.strip(),
                validation_label=payload.validation_label,
                candidate_tables=[str(item) for item in payload.candidate_tables],
                candidate_score_reasons=payload.candidate_score_reasons,
                comment=payload.comment.strip(),
                result_row_count=payload.result_row_count,
                had_execution_result=payload.had_execution_result,
            )
        except ValueError as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)

    @app.post("/admin/feedback-validation", response_model=FeedbackResponse)
    async def feedback_validation(payload: FeedbackValidationRequest):
        await asyncio.to_thread(release_gate.validate)
        if payload.validation_label not in {"correct", "incorrect"}:
            return JSONResponse(
                {
                    "success": False,
                    "error": "validation_label 必须为 correct 或 incorrect",
                },
                status_code=400,
            )

        try:
            executor, repository, schema_repository, artifact_provider = await asyncio.gather(
                asyncio.to_thread(getattr, container, "sql_executor"),
                asyncio.to_thread(getattr, container, "feedback_repository"),
                asyncio.to_thread(getattr, container, "schema_repository"),
                asyncio.to_thread(getattr, container, "artifact_provider"),
            )
            result = await review_feedback(
                question=payload.question.strip(),
                sql=payload.sql.strip(),
                candidate_tables=[str(item) for item in payload.candidate_tables],
                candidate_score_reasons=payload.candidate_score_reasons,
                validation_label=payload.validation_label,
                comment=payload.comment.strip(),
                result_row_count=payload.result_row_count,
                had_execution_result=payload.had_execution_result,
                reviewer=(
                    "admin-key:"
                    + hashlib.sha256(config.feedback_admin_api_key.encode("utf-8")).hexdigest()[:12]
                    if config.feedback_admin_api_key
                    else "local-admin"
                ),
                config=config,
                sql_executor=executor,
                feedback_repository=repository,
                schema_repository=schema_repository,
                artifact_provider=artifact_provider,
            )
        except Exception as exc:
            logger.warning("在线反馈提交失败: %s", exc)
            return JSONResponse(
                {"success": False, "error": "反馈未通过校验或暂时无法保存"},
                status_code=400 if isinstance(exc, ValueError) else 500,
            )

        return result

    return app


def run_server(config: Settings | None = None) -> None:
    """Initialize runtime adapters and serve the application."""
    settings = config or load_settings()
    container = ApplicationContainer(settings)
    app = create_app(settings, container)
    uvicorn_config = uvicorn.Config(
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

    server = QuietUvicornServer(uvicorn_config)
    with suppress(KeyboardInterrupt):
        server.run()
