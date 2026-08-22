"""Header and short-lived HttpOnly-cookie authentication for API and browser clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..core.config import Settings
from .schemas import AuthSessionRequest, AuthSessionResponse

SESSION_COOKIE = "text2sql_session"


@dataclass(frozen=True)
class SessionPrincipal:
    role: str
    expires_at: int


class SessionCodec:
    def __init__(self, secret: str | None, ttl_seconds: int):
        self._secret = secret.encode("utf-8") if secret else None
        self._ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._secret is not None

    def issue(self, role: str) -> str:
        if self._secret is None:
            raise RuntimeError("WEB_SESSION_SECRET is not configured")
        payload = {"role": role, "exp": int(time.time()) + self._ttl_seconds}
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"

    def read(self, token: str | None) -> SessionPrincipal | None:
        if not token or self._secret is None:
            return None
        try:
            encoded_text, signature_text = token.split(".", 1)
            encoded = encoded_text.encode("ascii")
            signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
            expected = hmac.new(self._secret, encoded, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
            role = str(payload.get("role") or "")
            expires_at = int(payload.get("exp") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if role not in {"user", "admin"} or expires_at <= int(time.time()):
            return None
        return SessionPrincipal(role=role, expires_at=expires_at)


def session_principal(request: Request, codec: SessionCodec) -> SessionPrincipal | None:
    return codec.read(request.cookies.get(SESSION_COOKIE))


def key_role(config: Settings, supplied_key: str) -> str | None:
    if config.feedback_admin_api_key and hmac.compare_digest(
        supplied_key, config.feedback_admin_api_key
    ):
        return "admin"
    if config.api_key and hmac.compare_digest(supplied_key, config.api_key):
        return "user"
    return None


def header_matches(request: Request, name: str, expected: str | None) -> bool:
    supplied = request.headers.get(name)
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


class ProductionAuthMiddleware(BaseHTTPMiddleware):
    """Protect query routes and keep Gold promotion fail-closed in every environment."""

    PROTECTED_PREFIXES = (
        "/ask",
        "/generate-sql",
        "/feedback",
        "/admin/feedback-validation",
        "/training-report",
    )

    def __init__(self, app, *, config: Settings, session_codec: SessionCodec):
        super().__init__(app)
        self._config = config
        self._session_codec = session_codec

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/admin/feedback-validation"):
            if not self._config.feedback_admin_api_key:
                return JSONResponse(
                    {"success": False, "error": "生产环境未配置反馈审核密钥"},
                    status_code=503,
                )
            principal = session_principal(request, self._session_codec)
            if not header_matches(
                request, "x-admin-api-key", self._config.feedback_admin_api_key
            ) and not (principal and principal.role == "admin"):
                return JSONResponse(
                    {"success": False, "error": "无效或缺失的反馈审核凭据"},
                    status_code=403,
                )
            return await call_next(request)

        if self._config.api_key and path.startswith(self.PROTECTED_PREFIXES):
            principal = session_principal(request, self._session_codec)
            if not header_matches(request, "x-api-key", self._config.api_key) and not principal:
                return JSONResponse(
                    {"success": False, "error": "无效或缺失的 API Key"},
                    status_code=401,
                )
            if (
                principal
                and request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse(
                    {"success": False, "error": "拒绝跨站会话请求"},
                    status_code=403,
                )
        return await call_next(request)


def install_auth_routes(app: FastAPI, config: Settings, session_codec: SessionCodec) -> None:
    @app.get("/auth/session", response_model=AuthSessionResponse)
    async def auth_session_status(request: Request):
        principal = session_principal(request, session_codec)
        return {
            "authenticated": principal is not None,
            "role": principal.role if principal else None,
            "expires_at": principal.expires_at if principal else None,
        }

    @app.post("/auth/session", response_model=AuthSessionResponse)
    async def create_auth_session(payload: AuthSessionRequest):
        if not session_codec.enabled or not config.api_key:
            return JSONResponse(
                {"authenticated": False, "role": None, "expires_at": None},
                status_code=503,
            )
        role = key_role(config, payload.api_key)
        if role is None:
            return JSONResponse(
                {"authenticated": False, "role": None, "expires_at": None},
                status_code=401,
            )
        token = session_codec.issue(role)
        principal = session_codec.read(token)
        assert principal is not None
        response = JSONResponse(
            {"authenticated": True, "role": role, "expires_at": principal.expires_at}
        )
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=config.web_session_ttl_seconds,
            httponly=True,
            secure=config.web_session_cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.delete("/auth/session", response_model=AuthSessionResponse)
    async def delete_auth_session():
        response = JSONResponse({"authenticated": False, "role": None, "expires_at": None})
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
