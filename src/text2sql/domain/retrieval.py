"""Pure retrieval results shared by application ports and adapters."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TableCandidate:
    table_name: str
    raw_score: float
    probability: float | None
    source: str
    reason: str
    is_bridge: bool = False


@dataclass(frozen=True)
class RetrievalSelection:
    candidates: tuple[TableCandidate, ...]
    diagnostics: dict[str, Any]
