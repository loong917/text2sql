import unittest

from src.application.text2sql_service import (
    Text2SQLDependencies,
    generate_sql_with_feedback,
)


class ApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependencies_can_be_injected_without_runtime_initialization(self):
        calls = {"llm": 0}

        async def context_builder(question):
            return {
                "prompt": "",
                "insufficient_context": True,
                "insufficiency_reason": "unsupported metric",
                "candidate_tables": [],
            }

        async def llm_generator(message):
            calls["llm"] += 1
            return "SELECT 1"

        async def feedback_capture(*args, **kwargs):
            raise AssertionError("feedback must not be captured for refusal")

        dependencies = Text2SQLDependencies(
            sql_runner_provider=lambda: object(),
            context_builder=context_builder,
            sql_validator=lambda *args, **kwargs: None,
            llm_generator=llm_generator,
            feedback_capture=feedback_capture,
        )
        result = await generate_sql_with_feedback(
            "unsupported",
            execute_sql=False,
            dependencies=dependencies,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["refusal_reason"], "unsupported metric")
        self.assertEqual(calls["llm"], 0)
