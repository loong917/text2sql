import time

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from vanna.servers.fastapi import VannaFastAPIServer

from ..core.agent import get_agent, reset_agent
from ..core.config import settings
from ..core.lifecycle import install_signal_handlers, register_shutdown
from ..core.logging import setup_logging

logger = setup_logging("text2sql.server")


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


def run_server() -> None:
    agent = get_agent()

    install_signal_handlers()
    register_shutdown(reset_agent)

    vanna_app = VannaFastAPIServer(agent=agent)
    app = vanna_app.create_app()

    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "healthy", "agent": type(agent).__name__}

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
    server = uvicorn.Server(config)
    server.run()
