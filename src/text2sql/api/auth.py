"""Header and short-lived HttpOnly-cookie authentication for API and browser clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

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
        if not token or len(token) > 2048 or self._secret is None:
            return None
        try:
            encoded_text, signature_text = token.split(".", 1)
            encoded = encoded_text.encode("ascii")
            signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
            expected = hmac.new(self._secret, encoded, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
            if not isinstance(payload, dict) or set(payload) != {"role", "exp"}:
                return None
            role, expires_at = payload["role"], payload["exp"]
            if not isinstance(role, str) or type(expires_at) is not int:
                return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if role not in {"user", "admin"} or expires_at <= int(time.time()):
            return None
        return SessionPrincipal(role=role, expires_at=expires_at)


def session_principal(request: Request, codec: SessionCodec) -> SessionPrincipal | None:
    return codec.read(request.cookies.get(SESSION_COOKIE))


def key_role(config: Settings, supplied_key: str) -> str | None:
    if config.feedback_admin_api_key and hmac.compare_digest(
        supplied_key.encode("utf-8"), config.feedback_admin_api_key.encode("utf-8")
    ):
        return "admin"
    if config.api_key and hmac.compare_digest(
        supplied_key.encode("utf-8"), config.api_key.encode("utf-8")
    ):
        return "user"
    return None


def header_matches(request: Request, name: str, expected: str | None) -> bool:
    supplied = request.headers.get(name)
    return bool(
        supplied
        and expected
        and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


def same_origin_write(request: Request) -> bool:
    """Cookies require browser-origin proof; explicit API-key clients do not."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    source = request.headers.get("origin")
    if source is None:
        return fetch_site == "same-origin"
    source_origin = _origin(source)
    return source_origin is not None and source_origin == _origin(str(request.base_url))


def _csrf_rejection() -> JSONResponse:
    return JSONResponse({"success": False, "error": "拒绝缺少同源证明的会话请求"}, status_code=403)


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
            header_authenticated = header_matches(
                request, "x-admin-api-key", self._config.feedback_admin_api_key
            )
            if not header_authenticated and not (principal and principal.role == "admin"):
                return JSONResponse(
                    {"success": False, "error": "无效或缺失的反馈审核凭据"},
                    status_code=403,
                )
            if not header_authenticated and not same_origin_write(request):
                return _csrf_rejection()
            return await call_next(request)

        if self._config.api_key and path.startswith(self.PROTECTED_PREFIXES):
            principal = session_principal(request, self._session_codec)
            header_authenticated = header_matches(request, "x-api-key", self._config.api_key)
            if not header_authenticated and not principal:
                return JSONResponse(
                    {"success": False, "error": "无效或缺失的 API Key"},
                    status_code=401,
                )
            if not header_authenticated and not same_origin_write(request):
                return _csrf_rejection()
        return await call_next(request)


def install_auth_routes(app: FastAPI, config: Settings, session_codec: SessionCodec) -> None:
    @app.get("/auth/session", response_model=AuthSessionResponse)
    async def auth_session_status(request: Request):
        principal = session_principal(request, session_codec)
        return JSONResponse(
            {
                "authenticated": principal is not None,
                "role": principal.role if principal else None,
                "expires_at": principal.expires_at if principal else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/auth/session", response_model=AuthSessionResponse)
    async def create_auth_session(payload: AuthSessionRequest, request: Request):
        if (
            request.headers.get("origin") is not None
            or request.headers.get("sec-fetch-site") is not None
        ) and not same_origin_write(request):
            return _csrf_rejection()
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
    async def delete_auth_session(request: Request):
        if session_principal(request, session_codec) and not same_origin_write(request):
            return _csrf_rejection()
        response = JSONResponse({"authenticated": False, "role": None, "expires_at": None})
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
