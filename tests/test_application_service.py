import unittest

from text2sql.application.contracts import QueryContext, ValidationResult
from text2sql.application.query_config import QueryServiceConfig
from text2sql.application.text2sql_service import (
    Text2SQLDependencies,
    Text2SQLService,
)


class FakeRetriever:
    async def retrieve(self, question):
        return QueryContext(
            "", {}, insufficient_context=True, insufficiency_reason="unsupported metric"
        )


class FakeGenerator:
    calls = 0

    async def generate(self, prompt):
        self.calls += 1
        return "SELECT 1"


class FakeValidator:
    def validate(self, sql, live_schema, **context):
        return ValidationResult(True)


class FakeExecutor:
    async def execute(self, sql, *, timeout_seconds):
        raise AssertionError("refused requests must not execute")


class FakeRepository:
    async def capture(self, question, sql, candidate_tables, **evidence):
        raise AssertionError("refused requests must not be captured")


class ApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_ports_and_configuration_are_injected_without_runtime_initialization(self):
        generator = FakeGenerator()
        service = Text2SQLService(
            Text2SQLDependencies(
                retriever=FakeRetriever(),
                generator=generator,
                validator=FakeValidator(),
                executor=FakeExecutor(),
                repository=FakeRepository(),
            ),
            QueryServiceConfig(
                max_result_rows=100,
                query_timeout_seconds=5,
                feedback_min_result_rows=1,
            ),
        )
        result = await service.generate(
            "unsupported",
            execute_sql=False,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["refusal_reason"], "unsupported metric")
        self.assertEqual(generator.calls, 0)
