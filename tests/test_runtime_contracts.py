"""Behavioral contracts for model output, SQL cancellation and runtime paths."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from text2sql.application.contracts import QueryContext, ValidationResult
from text2sql.application.query_config import QueryServiceConfig
from text2sql.application.text2sql_service import Text2SQLDependencies, Text2SQLService
from text2sql.core.config import load_settings
from text2sql.core.exceptions import ConfigurationError
from text2sql.infrastructure.ollama_generator import OllamaSqlGenerator
from text2sql.infrastructure.query_adapters import VannaSqlExecutor


class ModelContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_schema_and_transport_close_are_enforced(self):
        client = Mock()
        client.chat = AsyncMock(
            return_value={
                "message": {
                    "content": json.dumps({"sql": "SELECT 1", "refused": False, "reason": ""})
                }
            }
        )
        client._client.aclose = AsyncMock()
        with patch(
            "text2sql.infrastructure.ollama_generator.ollama.AsyncClient", return_value=client
        ):
            generator = OllamaSqlGenerator(
                host="http://test",
                timeout_seconds=1,
                model="test",
                max_concurrency=1,
                num_ctx=2048,
                num_predict=128,
                keep_alive="0",
                refusal_token="REFUSE",
            )
        self.assertEqual(await generator.generate("prompt"), "SELECT 1")
        self.assertIn("sql", client.chat.call_args.kwargs["format"]["properties"])
        for response in (
            {"done_reason": "length", "message": {"content": '{"sql":"SELECT 1"}'}},
            {"message": {"content": '{"sql":"SELECT 1","refused":true}'}},
            {"message": {"content": '{"sql":"SELECT 1","refused":"false"}'}},
        ):
            client.chat.return_value = response
            with self.assertRaises(ValueError):
                await generator.generate("prompt")
        await generator.aclose()
        client._client.aclose.assert_awaited_once()


class _SlowRunner:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    async def run_sql(self, _args, _context):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.2)
            return [{"value": 1}]
        finally:
            with self.lock:
                self.active -= 1


class WorkerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_caller_does_not_release_running_worker_permit(self):
        runner = _SlowRunner()
        executor = VannaSqlExecutor(runner, max_concurrency=1)
        try:
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(
                        executor.execute("SELECT 1", timeout_seconds=1), timeout=0.04
                    )
            self.assertEqual(runner.peak, 1)
        finally:
            await executor.aclose(drain_timeout=1)
        self.assertEqual(runner.active, 0)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await executor.execute("SELECT 1", timeout_seconds=1)


class _Retriever:
    async def retrieve(self, _question):
        return QueryContext("grounded", {})


class _Validator:
    def validate(self, *_args, **_kwargs):
        return ValidationResult(True)


class UseCaseContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_model_output_is_not_a_transport_failure_or_refusal(self):
        executor = Mock(execute=AsyncMock())
        generator = Mock(generate=AsyncMock(side_effect=ValueError("truncated JSON")))
        retriever = Mock(
            retrieve=AsyncMock(
                return_value=QueryContext("grounded", {"Fact": {"columns": {"ID": {}}}})
            )
        )
        service = Text2SQLService(
            Text2SQLDependencies(retriever, generator, _Validator(), executor, Mock()),
            QueryServiceConfig(10, 1, 1),
        )
        result = await service.generate("query", max_retries=1)
        self.assertEqual(result["outcome"], "generation_failed")
        self.assertEqual(result["error_code"], "MODEL_OUTPUT_INVALID")
        self.assertEqual(generator.generate.await_count, 2)
        executor.execute.assert_not_awaited()

    async def test_broken_validator_contract_does_not_execute(self):
        executor = Mock(execute=AsyncMock())
        generator = Mock(generate=AsyncMock(return_value="SELECT ID FROM Fact"))
        retriever = Mock(
            retrieve=AsyncMock(
                return_value=QueryContext("grounded", {"Fact": {"columns": {"ID": {}}}})
            )
        )
        service = Text2SQLService(
            Text2SQLDependencies(
                retriever, generator, Mock(validate=Mock(return_value=None)), executor, Mock()
            ),
            QueryServiceConfig(10, 1, 1),
        )
        result = await service.generate("query")
        self.assertEqual(result["outcome"], "infrastructure_error")
        self.assertEqual(result["error_code"], "VALIDATOR_UNAVAILABLE")
        executor.execute.assert_not_awaited()

    async def test_missing_schema_fails_closed_even_in_generate_only_mode(self):
        generator = Mock(generate=AsyncMock(return_value="SELECT 1"))
        executor = Mock(execute=AsyncMock())
        service = Text2SQLService(
            Text2SQLDependencies(_Retriever(), generator, _Validator(), executor, Mock()),
            QueryServiceConfig(10, 1, 1),
        )
        result = await service.generate("query", execute_sql=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "infrastructure_error")
        self.assertEqual(result["error_code"], "SCHEMA_UNAVAILABLE")
        generator.generate.assert_not_awaited()
        executor.execute.assert_not_awaited()


class PathContractTests(unittest.TestCase):
    def test_runtime_paths_are_independent_of_package_install_location(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ", {"APP_DATA_DIR": directory, "KNOWLEDGE_DB_DIR": "runtime/knowledge"}
            ):
                config = load_settings()
        self.assertEqual(Path(config.knowledge_db_dir), Path(directory) / "runtime" / "knowledge")

    def test_misspelled_production_environment_is_not_development(self):
        with patch.dict("os.environ", {"APP_ENV": "prod"}):
            with self.assertRaises(ConfigurationError):
                load_settings()
