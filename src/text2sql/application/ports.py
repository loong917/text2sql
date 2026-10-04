"""Application boundary contracts implemented by infrastructure adapters."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..domain.retrieval import RetrievalSelection
from ..domain.semantic_ir import QueryPlan
from .contracts import ContextKnowledge, QueryContext, SchemaSnapshot, ValidationResult


@runtime_checkable
class Repository(Protocol):
    """Persist automatic execution evidence without promoting it to Gold."""

    async def capture(
        self,
        question: str,
        sql: str,
        candidate_tables: list[str],
        **evidence: Any,
    ) -> Any: ...


@runtime_checkable
class FeedbackRepository(Repository, Protocol):
    """Retrieve and review governed feedback tiers."""

    def search_gold(
        self,
        question: str,
        limit: int,
        *,
        current_schema_fingerprint: str | None = None,
    ) -> list[dict[str, str]]: ...

    def search_negative(self, question: str, limit: int = 2) -> list[dict[str, Any]]: ...

    def load_gold(
        self, *, current_schema_fingerprint: str | None = None
    ) -> list[dict[str, Any]]: ...

    def count(self, tier: str) -> int: ...

    def submit_review(
        self,
        *,
        question: str,
        sql: str,
        candidate_tables: list[str],
        candidate_score_reasons: dict[str, Any] | None,
        validation_label: str,
        comment: str = "",
        result_row_count: int = 0,
        had_execution_result: bool = False,
        reviewer: str = "unknown",
        promotion_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def submit_candidate_review(
        self,
        *,
        question: str,
        sql: str,
        validation_label: str,
        candidate_tables: list[str],
        candidate_score_reasons: dict[str, Any] | None,
        comment: str = "",
        result_row_count: int = 0,
        had_execution_result: bool = False,
    ) -> dict[str, Any]: ...


@runtime_checkable
class Retriever(Protocol):
    """Build grounded context for one natural-language question."""

    async def retrieve(self, question: str) -> QueryContext: ...


@runtime_checkable
class Generator(Protocol):
    """Generate a single SQL candidate from a grounded prompt."""

    async def generate(self, prompt: str) -> str: ...


@runtime_checkable
class Validator(Protocol):
    """Validate SQL against Schema and semantic intent."""

    def validate(
        self,
        sql: str,
        live_schema: dict[str, dict[str, Any]],
        **context: Any,
    ) -> ValidationResult: ...


@runtime_checkable
class SqlExecutor(Protocol):
    """Execute an already validated, row-limited SQL query."""

    async def execute(self, sql: str, *, timeout_seconds: float) -> Any: ...


class SchemaRepository(Protocol):
    async def get(self) -> SchemaSnapshot: ...


class KnowledgeMemory(Protocol):
    async def search(self, question: str, limit: int) -> list[str]: ...


class ArtifactProvider(Protocol):
    def get(self) -> ContextKnowledge: ...


class SemanticParser(Protocol):
    def parse(self, question: str, knowledge: ContextKnowledge) -> QueryPlan: ...


class SqlCompiler(Protocol):
    def compile(self, plan: QueryPlan) -> str | None: ...


class TableSelector(Protocol):
    async def retrieve_with_diagnostics(
        self,
        question: str,
        plan: QueryPlan,
        schema: dict[str, dict[str, Any]],
    ) -> RetrievalSelection: ...
