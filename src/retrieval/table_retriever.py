"""Select tables using embeddings, learned calibration, and Schema paths."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ollama

from ..domain.semantic_ir import QuestionSemanticIR
from ..knowledge.provenance import schema_fingerprint
from .calibrator import PlattCalibrator
from .schema_graph import bridge_tables
from .table_card import TableCard, build_table_cards


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
        self.client = ollama.AsyncClient(host=host, timeout=timeout_seconds)
        self.model = model
        self.keep_alive = keep_alive

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self.client.embed(
            model=self.model,
            input=list(texts),
            keep_alive=self.keep_alive,
        )
        return [list(vector) for vector in response["embeddings"]]


@dataclass(frozen=True)
class TableCandidate:
    table_name: str
    raw_score: float
    probability: float | None
    source: str
    reason: str
    is_bridge: bool = False


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
    ) -> None:
        self.embedder = embedder
        self.calibrator_path = Path(calibrator_path)
        self.token_budget = token_budget
        self.embedding_model = embedding_model
        self.require_calibration = require_calibration
        self.dataset_fingerprint = dataset_fingerprint
        self._schema_key = ""
        self._cards: list[TableCard] = []
        self._card_embeddings: list[list[float]] = []

    async def _ensure_index(self, schema: dict[str, dict[str, Any]]) -> None:
        key = schema_fingerprint(schema)
        if key == self._schema_key:
            return
        self._cards = build_table_cards(schema)
        self._card_embeddings = await self.embedder.embed([card.text for card in self._cards])
        self._schema_key = key

    async def score_all(
        self,
        question: str,
        semantic_ir: QuestionSemanticIR,
        schema: dict[str, dict[str, Any]],
    ) -> list[tuple[TableCard, float]]:
        await self._ensure_index(schema)
        query = (
            question
            + "\n语义IR:"
            + json.dumps(semantic_ir.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        query_embedding = (await self.embedder.embed([query]))[0]
        scored = [
            (card, _cosine(query_embedding, embedding))
            for card, embedding in zip(self._cards, self._card_embeddings, strict=True)
        ]
        return sorted(scored, key=lambda item: (-item[1], item[0].table_name))

    async def retrieve(
        self,
        question: str,
        semantic_ir: QuestionSemanticIR,
        schema: dict[str, dict[str, Any]],
    ) -> list[TableCandidate]:
        if not semantic_ir.is_grounded:
            return []
        calibrator = PlattCalibrator.load(
            self.calibrator_path,
            expected_schema_fingerprint=schema_fingerprint(schema),
            expected_embedding_model=self.embedding_model,
            expected_dataset_fingerprint=self.dataset_fingerprint,
        )
        required = {table for table in semantic_ir.required_tables if table in schema}
        if calibrator is None and self.require_calibration:
            required_candidates = [
                TableCandidate(
                    table_name=table,
                    raw_score=0.0,
                    probability=None,
                    source="semantic_required",
                    reason="召回器未校准，仅允许语义 IR 明确要求的表",
                )
                for table in semantic_ir.required_tables
                if table in required
            ]
            selected_names = [item.table_name for item in required_candidates]
            for table_name in bridge_tables(schema, selected_names):
                required_candidates.append(
                    TableCandidate(
                        table_name=table_name,
                        raw_score=0.0,
                        probability=None,
                        source="schema_graph",
                        reason="连接语义必需表的最短外键路径",
                        is_bridge=True,
                    )
                )
            return required_candidates

        scored = await self.score_all(question, semantic_ir, schema)
        selected: list[TableCandidate] = []
        consumed_tokens = 0
        for card, raw_score in scored:
            probability = calibrator.predict(raw_score) if calibrator else None
            is_required = card.table_name in required
            accepted = is_required or (
                calibrator is not None
                and probability is not None
                and probability >= calibrator.threshold
            )
            if not calibrator and not self.require_calibration:
                accepted = True
            if not accepted:
                continue
            if not is_required and consumed_tokens + card.token_cost > self.token_budget:
                continue
            consumed_tokens += card.token_cost
            selected.append(
                TableCandidate(
                    table_name=card.table_name,
                    raw_score=raw_score,
                    probability=probability,
                    source="semantic_required" if is_required else "learned_retriever",
                    reason=(
                        "语义 IR 明确要求"
                        if is_required
                        else (
                            "通过验证集校准阈值" if calibrator else "未校准，按 token 预算保守召回"
                        )
                    ),
                )
            )

        selected_names = [item.table_name for item in selected]
        for table_name in bridge_tables(schema, selected_names):
            selected.append(
                TableCandidate(
                    table_name=table_name,
                    raw_score=0.0,
                    probability=None,
                    source="schema_graph",
                    reason="连接已选业务表的最短外键路径",
                    is_bridge=True,
                )
            )
        return selected
