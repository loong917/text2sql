"""Container-scoped mutable state for grounded query context."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..retrieval import TableRetriever


@dataclass
class ContextRuntimeState:
    """Caches owned by one application or offline job container."""

    live_schema: dict[str, dict[str, Any]] | None = None
    live_schema_cached_at: float = 0.0
    live_schema_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    knowledge_indexes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    table_retriever: TableRetriever | None = None
    semantic_catalog: Any | None = None

    def reset(self) -> None:
        self.live_schema = None
        self.live_schema_cached_at = 0.0
        self.knowledge_indexes.clear()
        self.table_retriever = None
        self.semantic_catalog = None
