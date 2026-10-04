"""Offline regressions for lazy ownership, async delivery and SQL preservation."""

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from text2sql.api.health import build_readiness
from text2sql.api.server import create_app
from text2sql.application.text2sql_service import _normalize_sql_output
from text2sql.bootstrap.container import ApplicationContainer
from text2sql.bootstrap.wiring import build_text2sql_service
from text2sql.core.config import load_settings
from text2sql.infrastructure.query_adapters import VannaSqlExecutor
from text2sql.infrastructure.runtime import RuntimeResources


class SqlTextPreservationTests(unittest.TestCase):
    def test_literal_spacing_and_line_comment_boundaries_are_not_rewritten(self):
        sql = "SELECT 'a  b' AS label -- explanation\nFROM Fact\nWHERE Note = 'x\ny'"
        self.assertEqual(_normalize_sql_output(sql), sql)
        self.assertEqual(_normalize_sql_output(f"```sql\n{sql}\n```"), sql)


class ContainerSingleFlightTests(unittest.TestCase):
    def test_concurrent_first_getters_publish_only_one_resource(self):
        container = ApplicationContainer(load_settings())
        created = object()
        entered = threading.Event()
        release = threading.Event()

        def build(*_args, **_kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release initializer")
            return created

        # Supply borrowed fixtures so this test opens no repository or DB.
        container.feedback_repository = object()
        container.sql_executor = object()
        with (
            patch(
                "text2sql.bootstrap.container.build_text2sql_service", side_effect=build
            ) as factory,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            first = workers.submit(lambda: container.query_service)
            try:
                self.assertTrue(entered.wait(1))
                second = workers.submit(lambda: container.query_service)
            finally:
                release.set()
            self.assertIs(first.result(2), created)
            self.assertIs(second.result(2), created)
            factory.assert_called_once()

    def test_failed_initialization_is_not_cached_and_shutdown_rejects_new_resources(self):
        container = ApplicationContainer(load_settings())
        factory = Mock(side_effect=[ValueError("bad factory"), object()])
        with self.assertRaises(ValueError):
            container._resource("fixture", factory)
        self.assertNotIn("fixture", container.__dict__)
        self.assertIs(container._resource("fixture", factory), container.__dict__["fixture"])
        container.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            container._resource("later", Mock())

    def test_explicit_candidate_index_requires_matching_memory_before_resource_creation(self):
        runtime = Mock()
        with self.assertRaisesRegex(ValueError, "matching versioned memory"):
            build_text2sql_service(
                load_settings(), runtime, knowledge_index_path="candidate/knowledge_index.json"
            )
        runtime.create_knowledge_memory.assert_not_called()


class RuntimeOwnershipTests(unittest.TestCase):
    def test_pinned_vendor_adapters_expose_owned_cleanup_resources(self):
        # Constructors are lazy: no collection, model or remote service is opened.
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
        from vanna.integrations.chromadb import ChromaAgentMemory

        embedding = OllamaEmbeddingFunction(
            url="http://localhost/api/embeddings", model_name="test"
        )
        memory = ChromaAgentMemory(
            persist_directory="unused-contract-fixture",
            collection_name="contract-fixture",
            embedding_function=embedding,
        )
        try:
            self.assertIsNone(memory._client)
            self.assertIsInstance(memory._executor, ThreadPoolExecutor)
            self.assertTrue(callable(embedding._client._client.close))
        finally:
            memory._executor.shutdown(wait=False, cancel_futures=True)
            embedding._client._client.close()

    def test_versioned_memory_is_reused_reported_and_closed_with_its_transport(self):
        runtime = RuntimeResources(load_settings())
        memory = SimpleNamespace(_executor=Mock())
        embedding = SimpleNamespace(_client=SimpleNamespace(_client=Mock()))
        with (
            patch.object(runtime, "_embedding_function", return_value=embedding) as embed_factory,
            patch(
                "text2sql.infrastructure.runtime.ChromaAgentMemory", return_value=memory
            ) as factory,
            patch("text2sql.infrastructure.runtime.KnowledgeArtifactRegistry") as registry,
        ):
            registry.return_value.load_active.return_value = SimpleNamespace(version="v1")
            self.assertIs(runtime.create_knowledge_memory(collection_name="v1"), memory)
            self.assertIs(runtime.create_knowledge_memory(collection_name="v1"), memory)
            self.assertEqual(runtime.status()["knowledge_memory"], "SimpleNamespace")
            factory.assert_called_once()
            embed_factory.assert_called_once()
        runtime.close()
        runtime.close()
        memory._executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        embedding._client._client.close.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            runtime.create_knowledge_memory(collection_name="v2")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            _ = runtime.sql_runner

    def test_memory_constructor_failure_closes_embedding_and_does_not_publish(self):
        runtime = RuntimeResources(load_settings())
        embedding = SimpleNamespace(_client=SimpleNamespace(_client=Mock()))
        with (
            patch.object(runtime, "_embedding_function", return_value=embedding),
            patch(
                "text2sql.infrastructure.runtime.ChromaAgentMemory", side_effect=ValueError("bad")
            ),
            self.assertRaises(ValueError),
        ):
            runtime.create_knowledge_memory(collection_name="v1")
        embedding._client._client.close.assert_called_once()
        self.assertEqual(runtime._memory_resources, {})

    def test_cleanup_failure_does_not_skip_other_pools_and_transports(self):
        runtime = RuntimeResources(load_settings())
        engine = Mock()
        engine.dispose.side_effect = OSError("database close")
        workers = Mock()
        workers.shutdown.side_effect = ValueError("worker close")
        transport = Mock()
        runtime._sql_runner = SimpleNamespace(engine=engine)
        runtime._memory_resources["v1"] = (
            SimpleNamespace(_executor=workers),
            SimpleNamespace(_client=SimpleNamespace(_client=transport)),
        )
        with self.assertRaises(ExceptionGroup) as caught:
            runtime.close()
        self.assertEqual(len(caught.exception.exceptions), 2)
        transport.close.assert_called_once()
        self.assertEqual(runtime._memory_resources, {})

    def test_temporary_model_probe_client_closes_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                runtime = RuntimeResources(load_settings())
                client = Mock()
                client.list.return_value = {"models": [{"name": "model", "digest": "sha"}]}
                if fail:
                    client.list.side_effect = OSError("offline")
                with patch("text2sql.infrastructure.runtime.ollama.Client", return_value=client):
                    if fail:
                        with self.assertRaises(OSError):
                            runtime.model_digests()
                    else:
                        self.assertEqual(runtime.model_digests(), {"model": "sha"})
                client._client.close.assert_called_once()


class AsyncDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_executor_drain_still_shuts_down_worker_pool(self):
        pool = Mock()
        with patch("text2sql.infrastructure.query_adapters.ThreadPoolExecutor", return_value=pool):
            executor = VannaSqlExecutor(object())
        pending = asyncio.get_running_loop().create_future()
        executor._jobs.add(pending)
        task = asyncio.create_task(executor.aclose())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        pending.cancel()

    async def test_training_report_loader_runs_outside_event_loop(self):
        loop_thread = threading.get_ident()
        callback_threads = []

        def load(_config):
            callback_threads.append(threading.get_ident())
            return {"available": False}

        config = replace(load_settings(), app_env="development")
        app = create_app(config, SimpleNamespace(close=Mock()))
        endpoint = next(route.endpoint for route in app.routes if route.path == "/training-report")
        with patch("text2sql.api.server.load_active_training_report", side_effect=load):
            self.assertEqual(await endpoint(), {"available": False})
        self.assertNotEqual(callback_threads, [loop_thread])

    async def test_readiness_io_and_lazy_executor_initialization_run_in_workers(self):
        loop_thread = threading.get_ident()
        callback_threads = []

        def mark(value):
            callback_threads.append(threading.get_ident())
            return value

        class Container:
            runtime = SimpleNamespace(
                probe_knowledge_collection=lambda _name: mark(None),
                probe_ollama=lambda: mark(None),
                status=lambda: mark({}),
            )

            @property
            def sql_executor(self):
                return mark(SimpleNamespace(execute=AsyncMock(return_value=[{"ready": 1}])))

        active = SimpleNamespace(version="v1", collection_name="v1")
        config = replace(load_settings(), table_retrieval_require_calibration=True)
        with (
            patch("text2sql.api.health.KnowledgeArtifactRegistry") as registry,
            patch(
                "text2sql.api.health._load_calibrator", side_effect=lambda *_args: mark(object())
            ),
        ):
            registry.return_value.load_active.side_effect = lambda: mark(active)
            payload, status = await build_readiness(config, Container())
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(len(callback_threads), 6)
        self.assertNotIn(loop_thread, callback_threads)
