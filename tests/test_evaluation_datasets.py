import json
from pathlib import Path
import unittest

from src.evaluation.dataset import assert_disjoint_splits, load_evaluation_cases
from src.retrieval.dataset import load_retrieval_examples


ROOT = Path(__file__).resolve().parents[1] / "evaluation"


class EvaluationDatasetTests(unittest.TestCase):
    def test_splits_are_valid_and_disjoint(self):
        train = load_evaluation_cases(
            ROOT / "retrieval_train.jsonl", expected_split="retrieval_train"
        )
        dev = load_evaluation_cases(ROOT / "dev.jsonl", expected_split="dev")
        test = load_evaluation_cases(ROOT / "test.jsonl", expected_split="test")
        assert_disjoint_splits(train, dev, test)

    def test_retriever_reads_only_training_split(self):
        examples = load_retrieval_examples(ROOT / "retrieval_train.jsonl")
        questions = {item.question for item in examples}
        held_out = {
            item.question
            for split in ("dev", "test")
            for item in load_evaluation_cases(
                ROOT / f"{split}.jsonl", expected_split=split
            )
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
            for item in load_evaluation_cases(
                ROOT / f"{split}.jsonl", expected_split=split
            )
        }
        self.assertTrue(gold_questions.isdisjoint(held_out))
