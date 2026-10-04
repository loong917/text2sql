"""Fail-closed validation for settings used by an internet-facing process."""

from __future__ import annotations

from .build_info import runtime_dependency_errors
from .config import Settings


def is_production(config: Settings) -> bool:
    return config.app_env == "production"


def production_configuration_errors(config: Settings) -> list[str]:
    if not is_production(config):
        return []

    errors: list[str] = runtime_dependency_errors()
    secrets = {
        "API_KEY": config.api_key,
        "FEEDBACK_ADMIN_API_KEY": config.feedback_admin_api_key,
        "WEB_SESSION_SECRET": config.web_session_secret,
    }
    for name, value in secrets.items():
        if not value or len(value) < 32:
            errors.append(f"{name} must contain at least 32 characters")
    configured_secrets = [value for value in secrets.values() if value]
    if len(configured_secrets) != len(set(configured_secrets)):
        errors.append("API, admin and session secrets must be pairwise distinct")
    if not 300 <= config.web_session_ttl_seconds <= 86_400:
        errors.append("WEB_SESSION_TTL_SECONDS must be between 300 and 86400")
    if not config.web_session_cookie_secure:
        errors.append("WEB_SESSION_COOKIE_SECURE must be true")
    if not config.app_revision or config.app_revision == "unknown":
        errors.append("APP_REVISION must identify the deployed commit")
    if len(config.llm_model_digest) < 32:
        errors.append("LLM_MODEL_DIGEST must pin the deployed Ollama model")
    if len(config.embedding_model_digest) < 32:
        errors.append("EMBEDDING_MODEL_DIGEST must pin the embedding model")
    if not config.sql_allowed_tables.strip():
        errors.append("SQL_ALLOWED_TABLES must be explicit in production")
    if not config.sql_denied_columns.strip():
        errors.append("SQL_DENIED_COLUMNS must protect sensitive columns in production")
    for name, minimum in (
        ("eval_min_pass_rate", 0.85),
        ("eval_min_positive_pass_rate", 0.85),
        ("eval_min_refusal_pass_rate", 0.95),
        ("eval_min_semantic_ir_pass_rate", 1.0),
        ("eval_min_execution_pass_rate", 0.85),
        ("eval_min_retrieval_recall", 0.9),
        ("production_eval_min_cases", 100),
        ("production_eval_min_positive_cases", 80),
        ("production_eval_min_refusal_cases", 20),
        ("production_retrieval_calibration_min_cases", 30),
        ("production_retrieval_test_min_cases", 30),
    ):
        if getattr(config, name) < minimum:
            errors.append(f"{name.upper()} must be at least {minimum} in production")
    return errors
