import json
from pathlib import Path
import tempfile
import unittest

from src.core.config import settings
from src.services.feedback_store import (
    capture_execution_feedback_sync,
    load_gold_examples,
    submit_online_feedback,
)


class FeedbackTierTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original = {
            name: getattr(settings, name)
            for name in (
                "feedback_examples_path",
                "feedback_pending_path",
                "feedback_negative_path",
                "feedback_review_path",
            )
        }
        for name in self.original:
            object.__setattr__(
                settings, name, str(Path(self.temp_dir.name) / f"{name}.jsonl")
            )

    def tearDown(self):
        for name, value in self.original.items():
            object.__setattr__(settings, name, value)
        self.temp_dir.cleanup()

    def _records(self, setting_name):
        path = Path(getattr(settings, setting_name))
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_execution_goes_to_pending_not_gold(self):
        capture_execution_feedback_sync(
            "统计宁波市采集人次",
            "SELECT COUNT(*) FROM Stat_Collection",
            ["Stat_Collection"],
            execution_succeeded=True,
            result_row_count=1,
        )
        self.assertEqual(len(self._records("feedback_pending_path")), 1)
        self.assertEqual(self._records("feedback_pending_path")[0]["feedback_tier"], "pending")
        self.assertEqual(self._records("feedback_examples_path"), [])

    def test_human_confirmation_promotes_and_removes_pending(self):
        question = "统计2025年全血采集人次"
        sql = "SELECT COUNT(*) FROM Stat_Collection WHERE BCType='0'"
        capture_execution_feedback_sync(
            question,
            sql,
            ["Stat_Collection"],
            execution_succeeded=True,
            result_row_count=1,
        )
        submit_online_feedback(
            question=question,
            sql=sql,
            candidate_tables=["Stat_Collection"],
            candidate_score_reasons={},
            validation_label="correct",
            result_row_count=1,
            had_execution_result=True,
        )
        self.assertEqual(self._records("feedback_pending_path"), [])
        gold = self._records("feedback_examples_path")
        self.assertEqual(len(gold), 1)
        self.assertTrue(gold[0]["user_validated"])
        self.assertEqual(gold[0]["feedback_tier"], "gold")

    def test_incorrect_feedback_goes_to_negative(self):
        submit_online_feedback(
            question="统计宁波市采集人次",
            sql="SELECT COUNT(*) FROM Stat_Collection",
            candidate_tables=["Stat_Collection"],
            candidate_score_reasons={},
            validation_label="incorrect",
            comment="missing_filter wrong_entity_value",
        )
        negative = self._records("feedback_negative_path")
        self.assertEqual(negative[0]["feedback_tier"], "negative")
        self.assertIn("missing_filter", negative[0]["error_types"])

    def test_legacy_execution_record_is_not_gold(self):
        path = Path(settings.feedback_examples_path)
        path.write_text(
            json.dumps(
                {
                    "question": "旧问题",
                    "sql": "SELECT 1",
                    "capture_source": "execution",
                    "quality_score": 100,
                    "approved": True,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(load_gold_examples(), [])


if __name__ == "__main__":
    unittest.main()
