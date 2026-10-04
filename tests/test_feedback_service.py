import unittest
from unittest.mock import patch

from tests.catalog_fixture import TEST_CATALOG
from tests.schema_fixture import synthetic_schema
from text2sql.application.contracts import ContextKnowledge, SchemaSnapshot
from text2sql.application.feedback_service import review_feedback
from text2sql.core.config import load_settings
from text2sql.knowledge.provenance import schema_fingerprint

settings = load_settings()
SCHEMA = synthetic_schema({"Fact": {"columns": {"ID": {}}}})


class Executor:
    async def execute(self, sql, *, timeout_seconds):
        return [{"total": 1}, {"total": 2}]


class Provider:
    def get(self):
        return ContextKnowledge(TEST_CATALOG, (), (), schema_fingerprint(SCHEMA), "test")


class SchemaRepository:
    async def get(self):
        return SchemaSnapshot(SCHEMA)


class FeedbackRepository:
    submitted = None

    def submit_review(self, **review):
        self.submitted = review
        return {"success": True}


class FeedbackServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_positive_review_uses_server_execution_evidence(self):
        repository = FeedbackRepository()
        with patch("text2sql.application.feedback_service.validate_sql", return_value=None):
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
                schema_repository=SchemaRepository(),
                artifact_provider=Provider(),
            )

        self.assertTrue(result["success"])
        kwargs = repository.submitted
        self.assertEqual(kwargs["result_row_count"], 2)
        self.assertTrue(kwargs["had_execution_result"])
        self.assertTrue(kwargs["promotion_evidence"]["execution_validated"])


if __name__ == "__main__":
    unittest.main()
