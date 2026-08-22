import json
import unittest
from pathlib import Path

from src.evaluation.dataset import (
    assert_disjoint_splits,
    load_evaluation_cases,
    sql_template_fingerprint,
)
from src.retrieval.dataset import load_retrieval_examples

ROOT = Path(__file__).resolve().parents[1] / "evaluation"


class EvaluationDatasetTests(unittest.TestCase):
    def test_sql_template_fingerprint_ignores_values_and_output_aliases(self):
        first = sql_template_fingerprint(
            "SELECT COUNT(*) AS Times FROM Fact WHERE City='杭州' AND Year=2024"
        )
        same_template = sql_template_fingerprint(
            "SELECT COUNT(*) AS Total FROM Fact WHERE City='宁波' AND Year=2025"
        )
        ranked_template = sql_template_fingerprint(
            "SELECT COUNT(*) AS Times FROM Fact WHERE City='杭州' AND Year=2024 "
            "ORDER BY COUNT(*) DESC"
        )
        self.assertEqual(first, same_template)
        self.assertNotEqual(first, ranked_template)

    def test_splits_are_valid_and_disjoint(self):
        train = load_evaluation_cases(
            ROOT / "retrieval_train.jsonl", expected_split="retrieval_train"
        )
        calibration = load_evaluation_cases(
            ROOT / "retrieval_calibration.jsonl",
            expected_split="retrieval_calibration",
        )
        retrieval_test = load_evaluation_cases(
            ROOT / "retrieval_test.jsonl", expected_split="retrieval_test"
        )
        dev = load_evaluation_cases(ROOT / "dev.jsonl", expected_split="dev")
        test = load_evaluation_cases(ROOT / "test.jsonl", expected_split="test")
        assert_disjoint_splits(train, calibration, retrieval_test, dev, test)

    def test_retrieval_threshold_and_quality_gate_use_held_out_splits(self):
        calibration = load_retrieval_examples(
            ROOT / "retrieval_calibration.jsonl",
            expected_split="retrieval_calibration",
        )
        test = load_retrieval_examples(
            ROOT / "retrieval_test.jsonl", expected_split="retrieval_test"
        )
        self.assertTrue(calibration)
        self.assertTrue(test)
        self.assertTrue(
            {item.question for item in calibration}.isdisjoint({item.question for item in test})
        )

    def test_retriever_reads_only_training_split(self):
        examples = load_retrieval_examples(ROOT / "retrieval_train.jsonl")
        questions = {item.question for item in examples}
        held_out = {
            item.question
            for split in ("dev", "test")
            for item in load_evaluation_cases(ROOT / f"{split}.jsonl", expected_split=split)
        }
        self.assertTrue(questions.isdisjoint(held_out))

    def test_held_out_questions_are_not_exact_gold_examples(self):
        gold_path = ROOT.parent / "knowledge" / "examples" / "gold_sql.jsonl"
        gold_questions = {
            " ".join(json.loads(line)["question"].lower().split())
            for line in gold_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        held_out = {
            " ".join(item.question.lower().split())
            for split in ("dev", "test")
            for item in load_evaluation_cases(ROOT / f"{split}.jsonl", expected_split=split)
        }
        self.assertTrue(gold_questions.isdisjoint(held_out))
