"""Human review and reproducible execution evidence; no automatic approval."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def sql_digest(sql: str) -> str:
    return sha256(sql.strip().encode("utf-8")).hexdigest()


def content_digest(payload: dict) -> str:
    """Bind approval to authored content, excluding lifecycle and evidence envelopes."""
    content = {
        key: value for key, value in payload.items() if key not in {"review", "execution", "status"}
    }
    return sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_min_length=1)
    source: str
    business_reviewer: str
    data_reviewer: str
    reviewed_at: str
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: str

    @field_validator("source", "business_reviewer", "data_reviewer", "notes")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("review text must be nonblank and have no surrounding whitespace")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("review timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def independent_reviewers(self) -> Self:
        if self.business_reviewer.strip().casefold() == self.data_reviewer.strip().casefold():
            raise ValueError("business and data reviewers must be independent")
        return self


class ExecutionEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_min_length=1)
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_snapshot_id: str
    environment: str
    executed_at: str
    success: Literal[True]
    truncated: Literal[False]
    row_count: int = Field(ge=0)
    result_columns: list[str] = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("success", "truncated", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("execution control flags must be explicit booleans")
        return value

    @field_validator("executed_at")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        return ReviewEvidence.aware_timestamp(value)

    @field_validator("result_columns")
    @classmethod
    def unique_columns(cls, values: list[str]) -> list[str]:
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("execution result columns must be unique")
        return values
