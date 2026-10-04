import unittest
from pathlib import Path

from tests.catalog_fixture import TEST_CATALOG
from text2sql.domain.semantic_contract import (
    SEMANTIC_SNAPSHOT_KEYS,
    build_semantic_snapshot,
    validate_semantic_snapshot,
)
from text2sql.domain.semantic_ir import parse_question_semantics
from text2sql.evaluation.dataset import load_evaluation_cases

ROOT = Path(__file__).resolve().parents[1] / "evaluation"


class EvaluationSemanticContractTests(unittest.TestCase):
    def test_snapshot_requires_complete_and_strict_current_evidence(self):
        snapshot = build_semantic_snapshot(
            parse_question_semantics("统计2025年采集量>=100的各机构", TEST_CATALOG)
        )
        self.assertEqual(set(snapshot), SEMANTIC_SNAPSHOT_KEYS)
        self.assertEqual(validate_semantic_snapshot(snapshot), [])
        invalid = (
            {"version": 3},
            snapshot | {"version": 2},
            snapshot | {"result_shape": {"limit": None}},
            snapshot | {"date_is_relative": "false"},
            snapshot | {"reference_date": "2025-01-01"},
            snapshot | {"date_range": {"start": "2025-01-01", "end": None}},
            snapshot | {"metric_predicates": [{"metric": "x", "operator": "gt", "value": 1}]},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertTrue(validate_semantic_snapshot(payload))

    def test_entity_inclusion_and_exclusion_have_distinct_semantic_evidence(self):
        included = build_semantic_snapshot(
            parse_question_semantics("统计杭州市2024年的全血采集人次", TEST_CATALOG)
        )
        excluded = build_semantic_snapshot(
            parse_question_semantics("统计排除杭州市2024年的全血采集人次", TEST_CATALOG)
        )
        included_city = next(
            item for item in included["entity_filter_intents"] if item["entity"] == "city"
        )
        excluded_city = next(
            item for item in excluded["entity_filter_intents"] if item["entity"] == "city"
        )
        self.assertEqual(included_city["operator"], "in")
        self.assertEqual(excluded_city["operator"], "not_in")
        self.assertNotEqual(included["entity_filter_intents"], excluded["entity_filter_intents"])

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
