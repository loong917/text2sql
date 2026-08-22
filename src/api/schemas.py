"""Stable HTTP request and response contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuthSessionRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class AuthSessionResponse(BaseModel):
    authenticated: bool
    role: str | None = None
    expires_at: int | None = None


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
    candidate_score_reasons: dict[str, Any] = Field(default_factory=dict)
    comment: str = ""
    result_row_count: int = Field(default=0, ge=0)
    had_execution_result: bool = False


class QueryResponse(BaseModel):
    success: bool
    question: str
    sql: Any = None
    result: Any = None
    attempts: int
    error: Any = None
    candidate_tables: list[str] = Field(default_factory=list)
    candidate_scores: dict[str, float] = Field(default_factory=dict)
    candidate_score_reasons: dict[str, Any] = Field(default_factory=dict)
    refusal_reason: str | None = None
    result_row_count: int = 0
    result_total_rows: int = 0
    result_truncated: bool = False
    result_columns: list[str] = Field(default_factory=list)


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    checks: dict[str, str]
    runtime: dict[str, str] | None = None
    actions: list[str] = Field(default_factory=list)


class TrainingReportResponse(BaseModel):
    success: bool
    available: bool
    error: str | None
    summary: dict[str, Any] | None
    report: dict[str, Any] | None
    manifest: dict[str, Any] | None


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool
    error: str | None = None
