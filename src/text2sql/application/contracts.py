"""Typed application values shared by use cases and their adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..domain.semantic_ir import QueryPlan, SemanticCatalog
from ..knowledge.provenance import schema_fingerprint

QueryOutcome = Literal[
    "success",
    "refused",
    "clarification_required",
    "infrastructure_error",
    "validation_failed",
    "generation_failed",
]


@dataclass(frozen=True)
class SchemaSnapshot:
    tables: dict[str, dict[str, Any]]

    @property
    def fingerprint(self) -> str:
        return schema_fingerprint(self.tables)


@dataclass(frozen=True)
class ContextKnowledge:
    catalog: SemanticCatalog
    table_cards: tuple[dict[str, Any], ...]
    negative_examples: tuple[dict[str, Any], ...]
    schema_fingerprint: str
    fingerprint: str
    gold_examples: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class QueryContext:
    prompt: str
    live_schema: dict[str, dict[str, Any]]
    semantic_ir: QueryPlan | None = None
    candidate_tables: list[str] = field(default_factory=list)
    candidate_scores: dict[str, float] = field(default_factory=dict)
    candidate_score_reasons: dict[str, Any] = field(default_factory=dict)
    insufficient_context: bool = False
    insufficiency_reason: str = ""
    outcome: QueryOutcome = "refused"
    diagnostics: dict[str, Any] = field(default_factory=dict)
