"""Container-scoped mutable state for grounded query context."""

import asyncio
from _thread import RLock
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextRuntimeState:
    """Caches owned by one application or offline job container."""

    live_schema: dict[str, dict[str, Any]] | None = None
    live_schema_cached_at: float = 0.0
    live_schema_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    knowledge_indexes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    knowledge_index_lock: RLock = field(default_factory=RLock)

    def reset(self) -> None:
        self.live_schema = None
        self.live_schema_cached_at = 0.0
        with self.knowledge_index_lock:
            self.knowledge_indexes.clear()
