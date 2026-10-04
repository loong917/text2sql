import json
import tempfile
import unittest
from pathlib import Path

from tests.evaluation_fixture import evaluation_cases, evaluation_split_cases
from text2sql.evaluation.dataset import (
    assert_disjoint_splits,
    load_evaluation_cases,
    sql_template_fingerprint,
)
from text2sql.retrieval.dataset import load_retrieval_examples

ROOT = Path(__file__).resolve().parents[1] / "evaluation"


class EvaluationDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for split in ("dev", "test", "retrieval_train", "retrieval_calibration", "retrieval_test"):
            (self.root / f"{split}.jsonl").write_text(
                "".join(json.dumps(case) + "\n" for case in evaluation_split_cases(split)),
                encoding="utf-8",
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_semantic_expectations_require_the_complete_current_contract(self):
        case = evaluation_cases()[0]
        expectations = (
            {"version": 3},
            case["expected_semantic_ir"] | {"version": 2},
            case["expected_semantic_ir"] | {"result_shape": {"limit": None}},
        )
        for semantic in expectations:
            with self.subTest(semantic=semantic), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "test.jsonl"
                path.write_text(
                    json.dumps(case | {"expected_semantic_ir": semantic}) + "\n", encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_evaluation_cases(path, expected_split="test")

    def test_control_flags_and_comparison_options_are_strict(self):
        mutations = (
            {"should_refuse": "false"},
            {"must_execute": "true"},
            {"expected_outcome": "infrastructure_error"},
            {"result_comparison": {"mode": "set"}},
            {"result_comparison": {"absolute_tolerance": -1}},
            {"must_include_columns": "Value"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cases.jsonl"
                path.write_text(
                    json.dumps(evaluation_cases()[0] | mutation) + "\n", encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    load_evaluation_cases(path, expected_split="test")

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
            self.root / "retrieval_train.jsonl", expected_split="retrieval_train"
        )
        calibration = load_evaluation_cases(
            self.root / "retrieval_calibration.jsonl",
            expected_split="retrieval_calibration",
        )
        retrieval_test = load_evaluation_cases(
            self.root / "retrieval_test.jsonl", expected_split="retrieval_test"
        )
        dev = load_evaluation_cases(self.root / "dev.jsonl", expected_split="dev")
        test = load_evaluation_cases(self.root / "test.jsonl", expected_split="test")
        assert_disjoint_splits(train, calibration, retrieval_test, dev, test)

    def test_retrieval_threshold_and_quality_gate_use_held_out_splits(self):
        calibration = load_retrieval_examples(
            self.root / "retrieval_calibration.jsonl",
            expected_split="retrieval_calibration",
        )
        test = load_retrieval_examples(
            self.root / "retrieval_test.jsonl", expected_split="retrieval_test"
        )
        self.assertTrue(calibration)
        self.assertTrue(test)
        self.assertTrue(
            {item.question for item in calibration}.isdisjoint({item.question for item in test})
        )

    def test_retriever_reads_only_training_split(self):
        examples = load_retrieval_examples(self.root / "retrieval_train.jsonl")
        questions = {item.question for item in examples}
        held_out = {
            item.question
            for split in ("dev", "test")
            for item in load_evaluation_cases(self.root / f"{split}.jsonl", expected_split=split)
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
            for item in load_evaluation_cases(self.root / f"{split}.jsonl", expected_split=split)
        }
        self.assertTrue(gold_questions.isdisjoint(held_out))
