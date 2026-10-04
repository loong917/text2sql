"""A cold knowledge index must not perform filesystem reads on the event loop."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from text2sql.application.context_rendering import warm_knowledge_index
from text2sql.application.context_service import ContextServiceConfig, build_prompt_context
from text2sql.application.context_state import ContextRuntimeState
from text2sql.application.contracts import ContextKnowledge, SchemaSnapshot
from text2sql.domain.retrieval import RetrievalSelection, TableCandidate
from text2sql.domain.semantic_ir import parse_question_semantics

from .catalog_fixture import TEST_CATALOG
from .schema_fixture import synthetic_schema
from .test_semantic_ast import SCHEMA


@pytest.mark.asyncio
async def test_cold_prompt_index_is_read_once_in_worker(tmp_path):
    index = tmp_path / "knowledge_index.json"
    index.write_text("[]", encoding="utf-8")
    state = ContextRuntimeState()
    schema = SchemaSnapshot(
        synthetic_schema(
            {
                table: {
                    **info,
                    "columns": {
                        column: {**details, "data_type": "int", "is_nullable": False}
                        for column, details in info["columns"].items()
                    },
                }
                for table, info in SCHEMA.items()
            }
        )
    )
    knowledge = ContextKnowledge(TEST_CATALOG, (), (), schema.fingerprint, "frozen")
    plan = parse_question_semantics("统计2025年采集量", TEST_CATALOG)
    event_thread = threading.get_ident()
    readers = []
    original = Path.read_text

    def record_read(path, *args, **kwargs):
        if path == index:
            readers.append(threading.get_ident())
        return original(path, *args, **kwargs)

    kwargs = dict(
        feedback_repository=Mock(),
        schema_repository=SimpleNamespace(get=AsyncMock(return_value=schema)),
        knowledge_memory=SimpleNamespace(search=AsyncMock(return_value=[])),
        artifact_provider=SimpleNamespace(get=lambda: knowledge),
        semantic_parser=SimpleNamespace(parse=lambda *_: plan),
        table_selector=SimpleNamespace(
            retrieve_with_diagnostics=AsyncMock(
                return_value=RetrievalSelection(
                    (TableCandidate("Stat_Collection", 1.0, None, "semantic", "required"),), {}
                )
            )
        ),
        state=state,
    )
    with patch.object(Path, "read_text", record_read):
        for _ in range(2):
            context = await build_prompt_context(
                plan.original_question, ContextServiceConfig(str(index), 2, 10000, False), **kwargs
            )
            assert context.semantic_ir == plan
    assert len(readers) == 1
    assert readers[0] != event_thread


def test_parallel_index_warmup_is_single_flight(tmp_path):
    index = tmp_path / "knowledge_index.json"
    index.write_text("[]", encoding="utf-8")
    state = ContextRuntimeState()
    with patch.object(Path, "read_text", return_value="[]") as read:
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(warm_knowledge_index, str(index), state) for _ in range(8)]
            for future in futures:
                future.result()
    assert read.call_count == 1
    state.reset()
    assert state.knowledge_indexes == {}
