"""Offline regressions for nonblocking review writes and complete shutdown."""

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.catalog_fixture import TEST_CATALOG
from tests.schema_fixture import synthetic_schema
from text2sql.application.contracts import ContextKnowledge, SchemaSnapshot
from text2sql.application.feedback_service import review_feedback
from text2sql.application.text2sql_service import Text2SQLService
from text2sql.bootstrap.container import ApplicationContainer
from text2sql.core.config import load_settings
from text2sql.infrastructure.query_adapters import CallableRetriever
from text2sql.infrastructure.runtime import RuntimeResources
from text2sql.knowledge.provenance import schema_fingerprint


def container_with_resources(service=None, executor=None):
    # Construct a lifecycle fixture without initializing any runtime clients.
    container = object.__new__(ApplicationContainer)
    container._resource_lock = threading.RLock()
    container._closed = False
    container.context_state = SimpleNamespace(reset=Mock())
    container.runtime = SimpleNamespace(close=Mock())
    if service is not None:
        container.query_service = service
    if executor is not None:
        container.sql_executor = executor
    return container


async def negative_review(repository):
    return await review_feedback(
        question="测试问题",
        sql="SELECT ID FROM Fact",
        validation_label="incorrect",
        reviewer="test",
        candidate_tables=["Fact"],
        candidate_score_reasons={},
        config=SimpleNamespace(),
        sql_executor=object(),
        feedback_repository=repository,
        schema_repository=object(),
        artifact_provider=object(),
    )


class FeedbackWriteContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_write_runs_on_worker_while_event_loop_remains_responsive(self):
        entered = threading.Event()
        release = threading.Event()
        writer_threads = []
        loop_thread = threading.get_ident()

        def submit_review(**review):
            writer_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release writer")
            return {"success": True, "reviewer": review["reviewer"]}

        task = asyncio.create_task(negative_review(SimpleNamespace(submit_review=submit_review)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            # This coroutine must advance while the synchronous writer is blocked.
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(len(writer_threads), 1)
            self.assertNotEqual(writer_threads[0], loop_thread)
        finally:
            release.set()
            result = await task
        self.assertEqual(result, {"success": True, "reviewer": "test"})

    async def test_review_write_failure_is_not_swallowed(self):
        repository = SimpleNamespace(submit_review=Mock(side_effect=OSError("database busy")))
        with self.assertRaisesRegex(OSError, "database busy"):
            await negative_review(repository)

    async def test_relative_date_positive_review_cannot_become_gold(self):
        schema = synthetic_schema({"Fact": {"columns": {"ID": {}}}})
        knowledge = ContextKnowledge(TEST_CATALOG, (), (), schema_fingerprint(schema), "test")
        repository = SimpleNamespace(submit_review=Mock())
        executor = SimpleNamespace(execute=AsyncMock())
        with (
            patch(
                "text2sql.application.feedback_service.parse_question_semantics",
                return_value=SimpleNamespace(date_is_relative=True),
            ),
            self.assertRaisesRegex(ValueError, "绝对日期"),
        ):
            await review_feedback(
                question="今年事实数量",
                sql="SELECT COUNT(*) FROM Fact",
                validation_label="correct",
                reviewer="test",
                candidate_tables=["Fact"],
                candidate_score_reasons={},
                config=load_settings(),
                sql_executor=executor,
                feedback_repository=repository,
                schema_repository=SimpleNamespace(
                    get=AsyncMock(return_value=SchemaSnapshot(schema))
                ),
                artifact_provider=SimpleNamespace(get=Mock(return_value=knowledge)),
            )
        executor.execute.assert_not_awaited()
        repository.submit_review.assert_not_called()


class ResourceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_closes_each_initialized_chroma_client_without_initializing_others(self):
        runtime = RuntimeResources(load_settings())
        client, transport, workers = Mock(), Mock(), Mock()
        runtime._memory_resources["initialized"] = (
            SimpleNamespace(_client=client, _executor=workers),
            SimpleNamespace(_client=SimpleNamespace(_client=transport)),
        )
        runtime._memory_resources["lazy"] = (
            SimpleNamespace(_client=None, _executor=Mock()),
            SimpleNamespace(_client=SimpleNamespace(_client=Mock())),
        )
        with patch("text2sql.infrastructure.runtime.chromadb.PersistentClient") as factory:
            runtime.close()
            runtime.close()
        client.close.assert_called_once()
        transport.close.assert_called_once()
        factory.assert_not_called()

    def test_runtime_chroma_close_failure_does_not_skip_other_owned_clients(self):
        runtime = RuntimeResources(load_settings())
        first, second = Mock(), Mock()
        first.close.side_effect = OSError("close failure")
        for name, client in (("first", first), ("second", second)):
            runtime._memory_resources[name] = (
                SimpleNamespace(_client=client, _executor=Mock()),
                SimpleNamespace(_client=SimpleNamespace(_client=Mock())),
            )
        with self.assertRaises(ExceptionGroup):
            runtime.close()
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertEqual(runtime._memory_resources, {})

    async def test_retriever_attempts_all_resources_and_aggregates_failures(self):
        first = SimpleNamespace(aclose=AsyncMock(side_effect=ValueError("first close")))
        second = SimpleNamespace(aclose=AsyncMock())
        third = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("third close")))
        retriever = CallableRetriever(AsyncMock(), resources=(first, second, third))
        with self.assertRaises(ExceptionGroup) as caught:
            await retriever.aclose()
        self.assertEqual(
            [type(error) for error in caught.exception.exceptions], [ValueError, OSError]
        )
        for resource in (first, second, third):
            resource.aclose.assert_awaited_once()

    async def test_query_service_failure_does_not_skip_retriever_close(self):
        generator = SimpleNamespace(aclose=AsyncMock(side_effect=ValueError("generator close")))
        retriever = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("retriever close")))
        service = Text2SQLService(SimpleNamespace(generator=generator, retriever=retriever), None)
        with self.assertRaises(ExceptionGroup) as caught:
            await service.aclose()
        self.assertEqual(len(caught.exception.exceptions), 2)
        generator.aclose.assert_awaited_once()
        retriever.aclose.assert_awaited_once()

    async def test_container_failure_does_not_skip_executor_or_runtime_close(self):
        service = SimpleNamespace(aclose=AsyncMock(side_effect=ValueError("service close")))
        executor = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("executor close")))
        container = container_with_resources(service, executor)
        container.runtime.close.side_effect = RuntimeError("runtime close")
        with self.assertRaises(ExceptionGroup) as caught:
            await container.aclose()
        self.assertEqual(len(caught.exception.exceptions), 3)
        executor.aclose.assert_awaited_once()
        container.context_state.reset.assert_called_once()
        container.runtime.close.assert_called_once()

    def test_state_reset_failure_does_not_skip_synchronous_runtime_close(self):
        container = container_with_resources()
        container.context_state.reset.side_effect = ValueError("reset close")
        with self.assertRaises(ExceptionGroup):
            container.close()
        container.runtime.close.assert_called_once()

    async def test_container_does_not_initialize_lazy_resources_during_close(self):
        container = container_with_resources()
        await container.aclose()
        self.assertNotIn("query_service", container.__dict__)
        self.assertNotIn("sql_executor", container.__dict__)
        container.runtime.close.assert_called_once()

    async def test_retriever_cancellation_preserves_signal_and_other_close_errors(self):
        first = SimpleNamespace(aclose=AsyncMock(side_effect=asyncio.CancelledError()))
        second = SimpleNamespace(aclose=AsyncMock(side_effect=ValueError("later close")))
        retriever = CallableRetriever(AsyncMock(), resources=(first, second))
        with self.assertRaises(asyncio.CancelledError) as caught:
            await retriever.aclose()
        second.aclose.assert_awaited_once()
        self.assertIsInstance(caught.exception.__cause__, ExceptionGroup)

    async def test_query_service_cancellation_does_not_skip_retriever(self):
        generator = SimpleNamespace(aclose=AsyncMock(side_effect=asyncio.CancelledError()))
        retriever = SimpleNamespace(aclose=AsyncMock())
        service = Text2SQLService(SimpleNamespace(generator=generator, retriever=retriever), None)
        with self.assertRaises(asyncio.CancelledError):
            await service.aclose()
        retriever.aclose.assert_awaited_once()

    async def test_external_cancellation_still_closes_container_resources(self):
        entered = asyncio.Event()

        async def close_service():
            entered.set()
            await asyncio.Event().wait()

        service = SimpleNamespace(aclose=close_service)
        executor = SimpleNamespace(aclose=AsyncMock())
        container = container_with_resources(service, executor)
        task = asyncio.create_task(container.aclose())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        executor.aclose.assert_awaited_once()
        container.runtime.close.assert_called_once()

    async def test_successful_close_preserves_generator_executor_runtime_order(self):
        order = []

        async def mark(name):
            order.append(name)

        service = Text2SQLService(
            SimpleNamespace(
                generator=SimpleNamespace(aclose=lambda: mark("generator")),
                retriever=SimpleNamespace(aclose=lambda: mark("retriever")),
            ),
            None,
        )
        executor = SimpleNamespace(aclose=lambda: mark("executor"))
        container = container_with_resources(service, executor)
        container.runtime.close = lambda: order.append("runtime")
        await container.aclose()
        self.assertEqual(order, ["generator", "retriever", "executor", "runtime"])
