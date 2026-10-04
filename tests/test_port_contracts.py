"""Application port behavior exercised through real adapters and fake clients."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
from vanna.capabilities.agent_memory import AgentMemory

from tests.schema_fixture import synthetic_schema
from text2sql.application.context_state import ContextRuntimeState
from text2sql.application.contracts import QueryContext, SchemaSnapshot, ValidationResult
from text2sql.application.ports import Generator, Retriever, SqlExecutor, Validator
from text2sql.infrastructure.context_adapters import SqlServerSchemaRepository, VannaKnowledgeMemory
from text2sql.infrastructure.query_adapters import CallableRetriever, CallableValidator
from text2sql.infrastructure.schema_repository import (
    LIVE_FK_QUERY,
    LIVE_KEY_QUERY,
    LIVE_SCHEMA_QUERY,
    LIVE_TABLE_QUERY,
)


class FakeSchemaExecutor:
    """Only metadata fixtures; this adapter cannot connect to a database."""

    def __init__(self, table="Fact"):
        self.table = table
        self.calls = []

    async def execute(self, sql, *, timeout_seconds):
        self.calls.append((sql, timeout_seconds))
        await asyncio.sleep(0)  # Exercise the cache single-flight lock.
        if sql == LIVE_TABLE_QUERY:
            return pd.DataFrame(
                [
                    {
                        "TABLE_NAME": self.table,
                        "SCHEMA_NAME": "dbo",
                        "OBJECT_ID": 1,
                        "TABLE_DESCRIPTION": "事实",
                    }
                ]
            )
        if sql == LIVE_SCHEMA_QUERY:
            return pd.DataFrame(
                [
                    {
                        "TABLE_NAME": self.table,
                        "SCHEMA_NAME": "dbo",
                        "COLUMN_NAME": "ID",
                        "DATA_TYPE": "int",
                        "IS_NULLABLE": False,
                        "MAX_LENGTH": 4,
                        "NUMERIC_PRECISION": 10,
                        "NUMERIC_SCALE": 0,
                        "COLLATION_NAME": None,
                        "IS_IDENTITY": False,
                        "IS_COMPUTED": False,
                        "COLUMN_DESCRIPTION": "编号",
                    }
                ]
            )
        if sql in (LIVE_FK_QUERY, LIVE_KEY_QUERY):
            return pd.DataFrame()
        raise AssertionError("Not a metadata query")


class PortContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_retriever_returns_typed_context_and_forwards_question(self):
        context = QueryContext(
            prompt="grounded",
            live_schema=synthetic_schema({"Fact": {"columns": {"ID": {}}}}),
            candidate_tables=["Fact"],
            outcome="success",
        )
        retrieve = AsyncMock(return_value=context)
        adapter = CallableRetriever(retrieve)
        self.assertIsInstance(adapter, Retriever)
        result = await adapter.retrieve("事实编号")
        self.assertIsInstance(result, QueryContext)
        self.assertIs(result, context)
        retrieve.assert_awaited_once_with("事实编号")

    def test_validator_returns_typed_success_and_failure_preserving_context(self):
        validate = Mock(side_effect=[None, "字段不存在"])
        adapter = CallableValidator(validate)
        self.assertIsInstance(adapter, Validator)
        schema = synthetic_schema({"Fact": {"columns": {"ID": {}}}})
        result = adapter.validate("SELECT ID FROM Fact", schema, question="编号")
        self.assertEqual(result, ValidationResult(valid=True))
        validate.assert_called_with("SELECT ID FROM Fact", schema, question="编号")
        result = adapter.validate("SELECT Bad FROM Fact", schema, question="编号")
        self.assertEqual(result, ValidationResult(False, "SQL_VALIDATION_FAILED", "字段不存在"))

    async def test_retriever_closes_owned_embedding_resources(self):
        resources = (SimpleNamespace(aclose=AsyncMock()), SimpleNamespace(aclose=AsyncMock()))
        adapter = CallableRetriever(AsyncMock(), resources=resources)
        await adapter.aclose()
        for resource in resources:
            resource.aclose.assert_awaited_once()

    async def test_schema_repository_is_typed_single_flight_and_scope_isolated(self):
        first_executor = FakeSchemaExecutor()
        first_state = ContextRuntimeState()
        first = SqlServerSchemaRepository(first_executor, first_state, ttl_seconds=60)
        snapshots = await asyncio.gather(*(first.get() for _ in range(8)))
        self.assertTrue(all(isinstance(snapshot, SchemaSnapshot) for snapshot in snapshots))
        self.assertEqual(len(first_executor.calls), 4)
        self.assertTrue(all(snapshot.tables is snapshots[0].tables for snapshot in snapshots))
        self.assertEqual(snapshots[0].tables["Fact"]["columns"]["ID"]["data_type"], "int")

        second_executor = FakeSchemaExecutor("Other")
        second_state = ContextRuntimeState()
        second = SqlServerSchemaRepository(second_executor, second_state, ttl_seconds=60)
        other = await second.get()
        self.assertEqual(set(other.tables), {"Other"})
        self.assertNotEqual(snapshots[0].fingerprint, other.fingerprint)
        self.assertEqual(set(first_state.live_schema), {"Fact"})
        first_state.knowledge_indexes["first"] = [{"text": "cached"}]
        second_state.knowledge_indexes["second"] = [{"text": "independent"}]
        first_state.reset()
        self.assertEqual(first_state.knowledge_indexes, {})
        self.assertIsNone(first_state.live_schema)
        self.assertEqual(set(second_state.knowledge_indexes), {"second"})
        await first.get()
        self.assertEqual(len(first_executor.calls), 8)
        self.assertEqual(len(second_executor.calls), 4)

    async def test_expired_schema_cache_refreshes_without_reusing_stale_tables(self):
        executor = FakeSchemaExecutor()
        state = ContextRuntimeState()
        repository = SqlServerSchemaRepository(executor, state, ttl_seconds=1)
        await repository.get()
        executor.table = "Changed"
        state.live_schema_cached_at = 0.0
        changed = await repository.get()
        self.assertEqual(set(changed.tables), {"Changed"})
        self.assertEqual(len(executor.calls), 8)

    async def test_memory_adapter_preserves_order_deduplicates_and_respects_limit(self):
        rows = [
            SimpleNamespace(memory=SimpleNamespace(content=text)) for text in ("A", "A", "B", "C")
        ]
        memory = Mock(spec=AgentMemory)
        memory.search_text_memories = AsyncMock(return_value=rows)
        result = await VannaKnowledgeMemory(memory).search("编号", limit=2)
        self.assertEqual(result, ["A", "B"])
        kwargs = memory.search_text_memories.call_args.kwargs
        self.assertEqual(kwargs["query"], "编号")
        self.assertEqual(kwargs["limit"], 2)
        self.assertIs(kwargs["context"].agent_memory, memory)

    async def test_generator_and_executor_behavior_and_missing_method_rejection(self):
        class FakeGenerator:
            async def generate(self, prompt):
                return f"SELECT '{prompt}'"

        generator = FakeGenerator()
        executor = FakeSchemaExecutor()
        self.assertIsInstance(generator, Generator)
        self.assertIsInstance(executor, SqlExecutor)
        self.assertEqual(await generator.generate("ID"), "SELECT 'ID'")
        self.assertNotIsInstance(object(), Retriever)
        self.assertNotIsInstance(object(), Generator)
