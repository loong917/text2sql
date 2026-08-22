import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.infrastructure.feedback_repository import (
    FeedbackPolicy,
    SQLiteFeedbackRepository,
)
from src.knowledge.provenance import schema_fingerprint


class FeedbackTierTests(unittest.TestCase):
    EVIDENCE = {
        "ast_validated": True,
        "schema_validated": True,
        "semantic_validated": True,
        "execution_validated": True,
        "schema_fingerprint": "test-schema",
    }

    def setUp(self):
        temp_root = Path("build/test-feedback")
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.repository = SQLiteFeedbackRepository(
            Path(self.temp_dir.name) / "feedback.sqlite3",
            FeedbackPolicy(min_quality_score=75),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_schema_is_versioned(self):
        with closing(sqlite3.connect(self.repository._path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

    def test_execution_goes_to_pending_not_gold(self):
        self.repository.capture_sync(
            "统计宁波市采集人次",
            "SELECT COUNT(*) FROM Stat_Collection",
            ["Stat_Collection"],
            execution_succeeded=True,
            result_row_count=1,
        )
        self.assertEqual(self.repository.count("pending"), 1)
        self.assertEqual(self.repository.count("gold"), 0)

    def test_human_confirmation_promotes_and_removes_pending(self):
        question = "统计2025年全血采集人次"
        sql = "SELECT COUNT(*) FROM Stat_Collection WHERE BCType='0'"
        self.repository.capture_sync(
            question,
            sql,
            ["Stat_Collection"],
            execution_succeeded=True,
            result_row_count=1,
        )
        self.repository.submit_review(
            question=question,
            sql=sql,
            candidate_tables=["Stat_Collection"],
            candidate_score_reasons={},
            validation_label="correct",
            result_row_count=1,
            had_execution_result=True,
            reviewer="unit-test",
            promotion_evidence=self.EVIDENCE,
        )
        self.assertEqual(self.repository.count("pending"), 0)
        gold = self.repository.list_samples("gold")
        self.assertEqual(len(gold), 1)
        self.assertTrue(gold[0]["user_validated"])
        self.assertEqual(gold[0]["reviewer"], "unit-test")

    def test_correct_feedback_without_promotion_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "晋升门禁"):
            self.repository.submit_review(
                question="统计2025年全血采集人次",
                sql="SELECT COUNT(*) FROM Stat_Collection",
                candidate_tables=["Stat_Collection"],
                candidate_score_reasons={},
                validation_label="correct",
            )

    def test_correct_feedback_without_server_execution_evidence_is_rejected(self):
        evidence = {**self.EVIDENCE, "execution_validated": False}
        with self.assertRaisesRegex(ValueError, "晋升门禁"):
            self.repository.submit_review(
                question="统计2025年全血采集人次",
                sql="SELECT COUNT(*) FROM Stat_Collection",
                candidate_tables=["Stat_Collection"],
                candidate_score_reasons={},
                validation_label="correct",
                promotion_evidence=evidence,
            )

    def test_live_prompt_quarantines_gold_after_schema_change(self):
        reviewed_schema = {"Fact": {"columns": {"ID": {"data_type": "int"}}}}
        evidence = {
            **self.EVIDENCE,
            "schema_fingerprint": schema_fingerprint(reviewed_schema),
        }
        self.repository.submit_review(
            question="统计事实记录",
            sql="SELECT COUNT(*) FROM Fact",
            candidate_tables=["Fact"],
            candidate_score_reasons={},
            validation_label="correct",
            reviewer="unit-test",
            had_execution_result=True,
            promotion_evidence=evidence,
        )
        self.assertEqual(
            len(
                self.repository.load_gold(
                    current_schema_fingerprint=schema_fingerprint(reviewed_schema)
                )
            ),
            1,
        )
        changed_schema = {"Fact": {"columns": {"NewID": {"data_type": "int"}}}}
        self.assertEqual(
            self.repository.load_gold(
                current_schema_fingerprint=schema_fingerprint(changed_schema)
            ),
            [],
        )

    def test_incorrect_feedback_goes_to_negative(self):
        self.repository.submit_review(
            question="统计宁波市采集人次",
            sql="SELECT COUNT(*) FROM Stat_Collection",
            candidate_tables=["Stat_Collection"],
            candidate_score_reasons={},
            validation_label="incorrect",
            comment="missing_filter wrong_entity_value",
        )
        negative = self.repository.list_samples("negative")
        self.assertIn("missing_filter", negative[0]["error_types"])

    def test_duplicate_pending_capture_is_upserted(self):
        for rows in (1, 3):
            self.repository.capture_sync(
                "统计事实记录",
                "SELECT COUNT(*) FROM Fact",
                ["Fact"],
                execution_succeeded=True,
                result_row_count=rows,
            )
        pending = self.repository.list_samples("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["result_row_count"], 3)

    def test_end_user_feedback_never_promotes_gold(self):
        result = self.repository.submit_candidate_review(
            question="统计事实记录",
            sql="SELECT COUNT(*) FROM Fact",
            validation_label="correct",
            candidate_tables=["Fact"],
            candidate_score_reasons={},
            had_execution_result=True,
            result_row_count=1,
        )
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(self.repository.count("gold"), 0)


if __name__ == "__main__":
    unittest.main()
