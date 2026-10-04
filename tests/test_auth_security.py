"""Cookie-origin, malformed session and credential-type security contracts."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from text2sql.api.auth import (
    SESSION_COOKIE,
    ProductionAuthMiddleware,
    SessionCodec,
    install_auth_routes,
    key_role,
)
from text2sql.core.config import load_settings


@pytest.fixture
def auth_client():
    config = replace(
        load_settings(),
        app_env="test",
        api_key="user-test-key",
        feedback_admin_api_key="admin-test-key",
        web_session_secret="session-secret-for-security-contract-tests",
        web_session_cookie_secure=False,
    )
    codec = SessionCodec(config.web_session_secret, 3600)
    app = FastAPI()
    install_auth_routes(app, config, codec)
    app.add_middleware(ProductionAuthMiddleware, config=config, session_codec=codec)

    @app.post("/ask")
    @app.post("/admin/feedback-validation")
    async def protected_write():
        return {"success": True}

    with TestClient(app) as client:
        yield client, config, codec


@pytest.mark.parametrize("role,route", [("user", "/ask"), ("admin", "/admin/feedback-validation")])
@pytest.mark.parametrize(
    "headers,expected_status",
    [
        ({}, 403),
        ({"Origin": "http://evil.test"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://testserver.evil.test"}, 403),
        ({"Origin": "http://testserver:81"}, 403),
        ({"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "http://testserver", "Sec-Fetch-Site": "same-site"}, 403),
        ({"Origin": "http://testserver"}, 200),
        ({"Origin": "http://testserver:80"}, 200),
        ({"Sec-Fetch-Site": "same-origin"}, 200),
    ],
)
def test_cookie_writes_require_same_origin_for_both_roles(
    auth_client, role, route, headers, expected_status
):
    client, _, codec = auth_client
    client.cookies.set(SESSION_COOKIE, codec.issue(role))
    assert client.post(route, json={}, headers=headers).status_code == expected_status


@pytest.mark.parametrize(
    "route,header,key",
    [
        ("/ask", "x-api-key", "user-test-key"),
        ("/admin/feedback-validation", "x-admin-api-key", "admin-test-key"),
    ],
)
def test_explicit_keys_do_not_require_browser_cookie_origin(auth_client, route, header, key):
    client, _, codec = auth_client
    client.cookies.set(SESSION_COOKIE, codec.issue("user"))
    assert client.post(route, json={}, headers={header: key}).status_code == 200


def test_session_login_logout_and_status_origin_contract(auth_client):
    client, config, _ = auth_client
    assert (
        client.post(
            "/auth/session",
            json={"api_key": config.api_key},
            headers={"Origin": "http://evil.test"},
        ).status_code
        == 403
    )
    login = client.post(
        "/auth/session", json={"api_key": config.api_key}, headers={"Origin": "http://testserver"}
    )
    assert login.status_code == 200
    assert client.get("/auth/session").headers["cache-control"] == "no-store"
    assert client.delete("/auth/session").status_code == 403
    assert client.get("/auth/session").json()["authenticated"] is True
    assert (
        client.delete("/auth/session", headers={"Origin": "http://testserver"}).status_code == 200
    )


def test_unicode_credentials_fail_normally_not_as_server_errors(auth_client):
    client, config, _ = auth_client
    assert client.post("/auth/session", json={"api_key": "不正确的密钥"}).status_code == 401
    assert key_role(replace(config, api_key="合法测试密钥"), "合法测试密钥") == "user"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"role": "user", "exp": True},
        {"role": "user", "exp": "9999999999"},
        {"role": "user", "exp": 9999999999.0},
        {"role": "user", "exp": 9999999999, "extra": 1},
    ],
)
def test_signed_but_malformed_session_payload_is_rejected(auth_client, payload):
    _, config, codec = auth_client
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signature = hmac.new(config.web_session_secret.encode(), encoded, hashlib.sha256).digest()
    token = encoded.decode() + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
    assert codec.read(token) is None


def test_session_expiry_and_tampering_are_rejected(auth_client):
    _, _, codec = auth_client
    token = codec.issue("user")
    assert codec.read(token + "x") is None
    assert codec.read("a" * 2049) is None
    assert SessionCodec("test", -1).read(SessionCodec("test", -1).issue("user")) is None
    assert codec.read(token).expires_at > int(time.time())
