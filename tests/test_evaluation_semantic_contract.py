import unittest
from pathlib import Path

from src.domain.semantic_ir import parse_question_semantics
from src.evaluation.dataset import load_evaluation_cases
from src.evaluation.semantic_snapshot import build_semantic_snapshot
from tests.catalog_fixture import TEST_CATALOG

ROOT = Path(__file__).resolve().parents[1] / "evaluation"


class EvaluationSemanticContractTests(unittest.TestCase):
    def test_all_generation_cases_match_versioned_semantic_snapshot(self):
        for split in ("dev", "test"):
            for case in load_evaluation_cases(ROOT / f"{split}.jsonl", expected_split=split):
                expected = case.payload["expected_semantic_ir"]
                actual = build_semantic_snapshot(
                    parse_question_semantics(case.question, TEST_CATALOG)
                )
                with self.subTest(case=case.id):
                    for key, value in expected.items():
                        self.assertEqual(actual.get(key), value)


if __name__ == "__main__":
    unittest.main()
