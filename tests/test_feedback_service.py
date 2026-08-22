import unittest
from unittest.mock import patch

from src.application.context_state import ContextRuntimeState
from src.application.feedback_service import review_feedback
from src.core.config import load_settings
from tests.catalog_fixture import TEST_CATALOG

settings = load_settings()


class Executor:
    async def execute(self, sql, *, timeout_seconds):
        return [{"total": 1}, {"total": 2}]


class Bundle:
    def semantic_catalog(self):
        return TEST_CATALOG


class FeedbackRepository:
    submitted = None

    def submit_review(self, **review):
        self.submitted = review
        return {"success": True}


class FeedbackServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_positive_review_uses_server_execution_evidence(self):
        repository = FeedbackRepository()
        with (
            patch(
                "src.application.feedback_service.get_live_schema",
                return_value={"Fact": {"columns": {"ID": {}}}},
            ),
            patch(
                "src.application.feedback_service.load_validated_knowledge_bundle",
                return_value=Bundle(),
            ),
            patch("src.application.feedback_service.validate_sql", return_value=None),
        ):
            result = await review_feedback(
                question="统计事实记录",
                sql="SELECT COUNT(*) AS total FROM Fact",
                validation_label="correct",
                reviewer="admin",
                candidate_tables=["Fact"],
                candidate_score_reasons={},
                result_row_count=999,
                had_execution_result=False,
                config=settings,
                sql_executor=Executor(),
                feedback_repository=repository,
                context_state=ContextRuntimeState(),
            )

        self.assertTrue(result["success"])
        kwargs = repository.submitted
        self.assertEqual(kwargs["result_row_count"], 2)
        self.assertTrue(kwargs["had_execution_result"])
        self.assertTrue(kwargs["promotion_evidence"]["execution_validated"])


if __name__ == "__main__":
    unittest.main()
