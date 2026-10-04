"""Knowledge and Schema adapters; external clients stay outside application code."""

from __future__ import annotations

import uuid
from typing import Any

from vanna import ToolContext, User

from ..application.context_state import ContextRuntimeState
from ..application.contracts import ContextKnowledge, SchemaSnapshot
from ..application.ports import SqlExecutor
from ..domain.semantic_ir import QueryPlan, parse_question_semantics
from ..infrastructure.schema_repository import get_live_schema
from ..knowledge.provenance import schema_fingerprint
from ..knowledge.snapshot import ArtifactSnapshot


class VannaKnowledgeMemory:
    def __init__(self, memory: Any):
        self.memory = memory

    async def search(self, question: str, limit: int) -> list[str]:
        context = ToolContext(
            user=User(id="query", username="query"),
            conversation_id="knowledge-search",
            request_id=uuid.uuid4().hex,
            agent_memory=self.memory,
        )
        results = await self.memory.search_text_memories(
            query=question, context=context, limit=limit
        )
        return list(dict.fromkeys(item.memory.content for item in results))[:limit]


class SnapshotArtifactProvider:
    def __init__(self, snapshot: ArtifactSnapshot):
        self.snapshot = snapshot

    def get(self) -> ContextKnowledge:
        bundle = self.snapshot.knowledge
        return ContextKnowledge(
            bundle.semantic_catalog(),
            tuple(bundle.table_cards),
            tuple(item for item in bundle.negative_sql if item.get("status") == "approved"),
            schema_fingerprint(self.snapshot.schema),
            bundle.fingerprint,
            tuple(item for item in bundle.gold_sql if item.get("status") == "approved"),
        )


class SqlServerSchemaRepository:
    def __init__(self, executor: SqlExecutor, state: ContextRuntimeState, ttl_seconds: int):
        self.executor, self.state, self.ttl_seconds = executor, state, ttl_seconds

    async def get(self) -> SchemaSnapshot:
        tables = await get_live_schema(
            self.ttl_seconds,
            sql_executor=self.executor,
            state=self.state,
        )
        return SchemaSnapshot(tables)


class CatalogSemanticParser:
    def parse(self, question: str, knowledge: ContextKnowledge) -> QueryPlan:
        return parse_question_semantics(question, knowledge.catalog)
