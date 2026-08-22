"""HTTP-level test from request validation through response serialization."""

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.config import load_settings
from src.core.exceptions import ConfigurationError


class FakeContainer:
    query_service = object()

    def close(self):
        return None


class ApiEndToEndTests(unittest.TestCase):
    def test_invalid_request_uses_stable_error_contract(self):
        config = load_settings()
        with TestClient(create_app(config, FakeContainer())) as client:
            response = client.post(
                "/ask",
                headers={"x-request-id": "test-request-001"},
                json={"question": "", "max_retries": 9},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "REQUEST_VALIDATION_FAILED")
        self.assertEqual(response.json()["request_id"], "test-request-001")
        self.assertEqual(response.headers["x-request-id"], "test-request-001")
        self.assertIn("body.question", response.json()["fields"])

    def test_readiness_rejects_missing_active_knowledge_artifact(self):
        config = load_settings()
        with TestClient(create_app(config, FakeContainer())) as client:
            response = client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["checks"]["knowledge_artifact"],
            "missing_or_incomplete",
        )

    def test_ask_endpoint_preserves_application_response_contract(self):
        config = load_settings()
        payload = {
            "success": True,
            "question": "统计数量",
            "sql": "SELECT COUNT(*) AS total FROM fact",
            "result": [{"total": 2}],
            "attempts": 1,
            "error": None,
            "candidate_tables": ["fact"],
            "candidate_scores": {"fact": 0.9},
            "candidate_score_reasons": {},
            "refusal_reason": None,
            "result_row_count": 1,
            "result_total_rows": 1,
            "result_truncated": False,
            "result_columns": ["total"],
        }
        headers = {"x-api-key": config.api_key} if config.api_key else {}
        with patch(
            "src.api.server.generate_sql_with_feedback",
            new=AsyncMock(return_value=payload),
        ):
            with TestClient(create_app(config, FakeContainer())) as client:
                response = client.post(
                    "/ask",
                    headers=headers,
                    json={"question": "统计数量", "execute_sql": True},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)

    def test_feedback_promotion_is_disabled_without_separate_admin_key(self):
        config = replace(load_settings(), api_key="query-key", feedback_admin_api_key=None)
        with TestClient(create_app(config, FakeContainer())) as client:
            response = client.post(
                "/admin/feedback-validation",
                headers={"x-api-key": "query-key"},
                json={
                    "question": "统计数量",
                    "sql": "SELECT COUNT(*) FROM Fact",
                    "validation_label": "correct",
                },
            )

        self.assertEqual(response.status_code, 503)

    def test_feedback_promotion_is_disabled_when_all_keys_are_empty(self):
        config = replace(load_settings(), api_key=None, feedback_admin_api_key=None)
        with TestClient(create_app(config, FakeContainer())) as client:
            response = client.post(
                "/admin/feedback-validation",
                json={
                    "question": "统计数量",
                    "sql": "SELECT COUNT(*) FROM Fact",
                    "validation_label": "correct",
                },
            )

        self.assertEqual(response.status_code, 503)

    def test_browser_session_uses_httponly_cookie_for_query(self):
        config = replace(
            load_settings(),
            api_key="query-key-with-sufficient-test-entropy",
            feedback_admin_api_key="admin-key-with-sufficient-test-entropy",
            web_session_secret="session-secret-with-sufficient-test-entropy",
            web_session_cookie_secure=False,
        )
        payload = {
            "success": True,
            "question": "统计数量",
            "sql": "SELECT COUNT(*) AS total FROM fact",
            "result": [{"total": 2}],
            "attempts": 1,
            "error": None,
            "candidate_tables": ["fact"],
            "candidate_scores": {"fact": 0.9},
            "candidate_score_reasons": {},
            "refusal_reason": None,
            "result_row_count": 1,
            "result_total_rows": 1,
            "result_truncated": False,
            "result_columns": ["total"],
        }
        with patch(
            "src.api.server.generate_sql_with_feedback",
            new=AsyncMock(return_value=payload),
        ):
            with TestClient(create_app(config, FakeContainer())) as client:
                login = client.post(
                    "/auth/session",
                    json={"api_key": "query-key-with-sufficient-test-entropy"},
                )
                response = client.post(
                    "/ask",
                    json={"question": "统计数量", "execute_sql": True},
                )

        self.assertEqual(login.status_code, 200)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertEqual(response.status_code, 200)

    def test_production_server_rejects_incomplete_security_configuration(self):
        config = replace(load_settings(), app_env="production", api_key=None)
        with self.assertRaises(ConfigurationError):
            create_app(config, FakeContainer())
