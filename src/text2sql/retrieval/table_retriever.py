"""Select tables using embeddings, learned calibration, and Schema paths."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ollama

from ..core.http_policy import ollama_http_options
from ..domain.retrieval import RetrievalSelection, TableCandidate
from ..domain.semantic_ir import QueryPlan
from ..knowledge.provenance import schema_fingerprint
from .calibrator import PlattCalibrator
from .schema_graph import bridge_tables
from .table_card import TableCard, build_table_cards, business_cards_fingerprint


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(
        self,
        *,
        host: str,
        timeout_seconds: float,
        model: str,
        keep_alive: str,
    ) -> None:
        self.client = ollama.AsyncClient(
            host=host, timeout=timeout_seconds, **ollama_http_options(host)
        )
        self.model = model
        self.keep_alive = keep_alive

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self.client.embed(
            model=self.model,
            input=list(texts),
            keep_alive=self.keep_alive,
        )
        return [list(vector) for vector in response["embeddings"]]

    async def aclose(self) -> None:
        """Close Ollama's HTTP transport; the SDK exposes no public close yet."""
        await self.client._client.aclose()


@dataclass(frozen=True)
class _IndexSnapshot:
    key: str
    cards: tuple[TableCard, ...]
    embeddings: tuple[tuple[float, ...], ...]


def select_candidates(
    semantic_ir: QueryPlan,
    schema: dict[str, dict[str, Any]],
    cards: Sequence[TableCard],
    scored: Sequence[tuple[TableCard, float]],
    calibrator: PlattCalibrator | None,
    *,
    token_budget: int,
    require_calibration: bool,
) -> RetrievalSelection:
    """Apply one selection policy in online retrieval and held-out evaluation."""
    diagnostics: dict[str, Any] = {
        "token_budget": token_budget,
        "consumed_tokens": 0,
        "budget_exceeded": False,
        "semantic_grounded": semantic_ir.is_grounded,
        "calibrator_available": calibrator is not None,
        "token_cost_basis": "table_card_character_estimate",
    }
    if not semantic_ir.is_grounded:
        return RetrievalSelection((), diagnostics)
    by_name = {card.table_name: card for card in cards}
    score_by_name = {card.table_name: score for card, score in scored}
    required = list(
        dict.fromkeys(table for table in semantic_ir.required_tables if table in schema)
    )
    selected: list[TableCandidate] = []
    selected_names: set[str] = set()
    consumed_tokens = 0

    def append(table_name: str, source: str, reason: str, *, bridge: bool = False) -> None:
        nonlocal consumed_tokens
        if table_name in selected_names:
            return
        score = score_by_name.get(table_name, 0.0)
        selected.append(
            TableCandidate(
                table_name,
                score,
                calibrator.predict(score) if calibrator else None,
                source,
                reason,
                bridge,
            )
        )
        selected_names.add(table_name)
        consumed_tokens += by_name[table_name].token_cost

    for table_name in required:
        append(table_name, "semantic_required", "语义 IR 明确要求")
    for table_name in bridge_tables(schema, required):
        append(table_name, "schema_graph", "连接语义必需表的最短外键路径", bridge=True)
    if calibrator is not None or not require_calibration:
        ranked = sorted(
            scored,
            key=lambda item: (
                -(calibrator.predict(item[1]) if calibrator else item[1]),
                item[0].table_name,
            ),
        )
        for card, score in ranked:
            if card.table_name in selected_names:
                continue
            if calibrator and calibrator.predict(score) < calibrator.threshold:
                continue
            proposed = [*(item.table_name for item in selected), card.table_name]
            bridges = [
                name for name in bridge_tables(schema, proposed) if name not in selected_names
            ]
            additional = card.token_cost + sum(by_name[name].token_cost for name in bridges)
            if consumed_tokens + additional > token_budget:
                continue
            append(
                card.table_name,
                "learned_retriever",
                "通过独立校准集概率阈值" if calibrator else "未校准，受 token 预算限制",
            )
            for table_name in bridges:
                append(table_name, "schema_graph", "连接已选业务表的最短外键路径", bridge=True)
    diagnostics.update(
        {
            "consumed_tokens": consumed_tokens,
            "budget_exceeded": consumed_tokens > token_budget,
            "missing_semantic_tables": [
                table for table in semantic_ir.required_tables if table not in schema
            ],
        }
    )
    return RetrievalSelection(tuple(selected), diagnostics)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


class TableRetriever:
    def __init__(
        self,
        embedder: Embedder,
        calibrator_path: str | Path,
        token_budget: int,
        embedding_model: str,
        require_calibration: bool = True,
        dataset_fingerprint: str | None = None,
        business_cards: Sequence[Mapping[str, Any]] = (),
        embedding_model_digest: str | None = None,
    ) -> None:
        self.embedder = embedder
        self.embedding_model_digest = embedding_model_digest
        self.calibrator_path = Path(calibrator_path)
        self.token_budget = token_budget
        self.embedding_model = embedding_model
        self.require_calibration = require_calibration
        self.dataset_fingerprint = dataset_fingerprint
        self.business_cards = tuple(deepcopy(dict(item)) for item in business_cards)
        self._index: _IndexSnapshot | None = None
        self._index_lock = asyncio.Lock()

    @property
    def business_card_fingerprint(self) -> str:
        return business_cards_fingerprint(self.business_cards)

    async def aclose(self) -> None:
        close = getattr(self.embedder, "aclose", None)
        if close is not None:
            await close()

    async def _ensure_index(self, schema: dict[str, dict[str, Any]]) -> _IndexSnapshot:
        key = f"{schema_fingerprint(schema)}:{business_cards_fingerprint(self.business_cards)}:{self.embedding_model}"
        if self._index and key == self._index.key:
            return self._index
        async with self._index_lock:
            if self._index and key == self._index.key:
                return self._index
            cards = tuple(build_table_cards(schema, self.business_cards))
            embeddings = tuple(
                tuple(vector) for vector in await self.embedder.embed([card.text for card in cards])
            )
            if len(cards) != len(embeddings):
                raise ValueError("表嵌入数量与 TableCard 数量不一致")
            snapshot = _IndexSnapshot(key, cards, embeddings)
            self._index = snapshot
            return snapshot

    async def score_all(
        self,
        question: str,
        semantic_ir: QueryPlan,
        schema: dict[str, dict[str, Any]],
    ) -> list[tuple[TableCard, float]]:
        snapshot = await self._ensure_index(schema)
        query = (
            question
            + "\n语义IR:"
            + json.dumps(semantic_ir.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        query_embedding = (await self.embedder.embed([query]))[0]
        scored = [
            (card, _cosine(query_embedding, embedding))
            for card, embedding in zip(snapshot.cards, snapshot.embeddings, strict=True)
        ]
        return sorted(scored, key=lambda item: (-item[1], item[0].table_name))

    async def retrieve(
        self,
        question: str,
        semantic_ir: QueryPlan,
        schema: dict[str, dict[str, Any]],
    ) -> list[TableCandidate]:
        result = await self.retrieve_with_diagnostics(question, semantic_ir, schema)
        return list(result.candidates)

    async def retrieve_with_diagnostics(
        self,
        question: str,
        semantic_ir: QueryPlan,
        schema: dict[str, dict[str, Any]],
    ) -> RetrievalSelection:
        calibrator = PlattCalibrator.load(
            self.calibrator_path,
            expected_schema_fingerprint=schema_fingerprint(schema),
            expected_embedding_model=self.embedding_model,
            expected_embedding_model_digest=self.embedding_model_digest,
            expected_dataset_fingerprint=self.dataset_fingerprint,
            expected_business_card_fingerprint=self.business_card_fingerprint
            if self.business_cards
            else None,
        )
        scored = []
        if semantic_ir.is_grounded and (calibrator is not None or not self.require_calibration):
            scored = await self.score_all(question, semantic_ir, schema)
        return select_candidates(
            semantic_ir,
            schema,
            build_table_cards(schema, self.business_cards),
            scored,
            calibrator,
            token_budget=self.token_budget,
            require_calibration=self.require_calibration,
        )
