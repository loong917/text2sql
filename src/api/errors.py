"""Consistent JSON errors without exposing infrastructure details."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from ..core.logging import setup_logging

logger = setup_logging("text2sql.api.errors")


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
        return JSONResponse(
            {
                "success": False,
                "error": "请求参数不符合接口约束",
                "error_code": "REQUEST_VALIDATION_FAILED",
                "request_id": getattr(request.state, "request_id", None),
                "fields": fields,
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.exception("Unhandled API error on %s", request.url.path, exc_info=exc)
        return JSONResponse(
            {
                "success": False,
                "error": "服务暂时不可用，请稍后重试",
                "error_code": "INTERNAL_ERROR",
                "request_id": getattr(request.state, "request_id", None),
            },
            status_code=500,
        )
