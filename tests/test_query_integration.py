"""Integration tests for the complete injected Text2SQL use case."""

import unittest

from tests.schema_fixture import synthetic_schema
from text2sql.application.contracts import QueryContext, ValidationResult
from text2sql.application.query_config import QueryServiceConfig
from text2sql.application.text2sql_service import Text2SQLDependencies, Text2SQLService


class Retriever:
    def __init__(self):
        self.calls = 0

    async def retrieve(self, question):
        self.calls += 1
        return QueryContext(
            "grounded",
            synthetic_schema({"fact": {"columns": {"id": {}}}}),
            candidate_tables=["fact"],
            candidate_scores={"fact": 0.98},
        )


class Generator:
    def __init__(self):
        self.calls = 0

    async def generate(self, prompt):
        self.calls += 1
        return "SELECT id FROM fact"


class Validator:
    def validate(self, sql, live_schema, **context):
        return ValidationResult(True)


class Executor:
    def __init__(self):
        self.sql = None

    async def execute(self, sql, *, timeout_seconds):
        self.sql = sql
        return [{"id": 1}, {"id": 2}, {"id": 3}]


class Repository:
    def __init__(self):
        self.evidence = None

    async def capture(self, question, sql, candidate_tables, **evidence):
        self.evidence = evidence


class QueryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_validation_execution_and_capture_form_one_pipeline(self):
        executor = Executor()
        repository = Repository()
        service = Text2SQLService(
            Text2SQLDependencies(
                retriever=Retriever(),
                generator=Generator(),
                validator=Validator(),
                executor=executor,
                repository=repository,
            ),
            QueryServiceConfig(
                max_result_rows=2,
                query_timeout_seconds=4,
                feedback_min_result_rows=1,
            ),
        )

        response = await service.generate("query")

        self.assertTrue(response["success"])
        self.assertTrue(response["result_truncated"])
        self.assertEqual(response["result_total_rows"], 3)
        self.assertEqual(response["result_row_count"], 2)
        self.assertIn("TOP 3", executor.sql.upper())
        self.assertEqual(repository.evidence["result_row_count"], 3)

    async def test_feedback_failure_does_not_fail_or_repeat_a_successful_query(self):
        class FailingRepository:
            async def capture(self, question, sql, candidate_tables, **evidence):
                raise OSError("disk unavailable")

        retriever = Retriever()
        generator = Generator()
        executor = Executor()
        service = Text2SQLService(
            Text2SQLDependencies(
                retriever=retriever,
                generator=generator,
                validator=Validator(),
                executor=executor,
                repository=FailingRepository(),
            ),
            QueryServiceConfig(10, 4, 1),
        )

        response = await service.generate("query", max_retries=2)

        self.assertTrue(response["success"])
        self.assertEqual(retriever.calls, 1)
        self.assertEqual(generator.calls, 1)

    async def test_execution_failure_is_not_retried_with_another_sql(self):
        class FailingExecutor:
            async def execute(self, sql, *, timeout_seconds):
                raise TimeoutError("database timeout")

        retriever = Retriever()
        generator = Generator()
        service = Text2SQLService(
            Text2SQLDependencies(
                retriever=retriever,
                generator=generator,
                validator=Validator(),
                executor=FailingExecutor(),
                repository=Repository(),
            ),
            QueryServiceConfig(10, 4, 1),
        )

        response = await service.generate("query", max_retries=2)

        self.assertFalse(response["success"])
        self.assertIn("SQL 执行失败", response["error"])
        self.assertEqual(retriever.calls, 1)
        self.assertEqual(generator.calls, 1)
