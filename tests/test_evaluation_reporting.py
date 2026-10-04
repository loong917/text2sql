import unittest
from copy import deepcopy

from tests.evaluation_fixture import evaluation_cases, evaluation_results
from text2sql.evaluation.reporting import evaluate_quality_gate, summarize_evaluation


class EvaluationReportingTests(unittest.TestCase):
    def test_execution_and_result_match_use_every_positive_case_as_the_denominator(self):
        positive, refusal = evaluation_results(evaluation_cases())
        generation_only = deepcopy(positive)
        generation_only.update(
            executed=False,
            baseline_compared=False,
            baseline_complete=False,
            baseline_succeeded=False,
        )
        generation_only["checks"] = [
            item
            for item in generation_only["checks"]
            if item["name"] != "execution_success" and not item["name"].startswith("baseline_")
        ]
        summary = summarize_evaluation([positive, *[generation_only] * 79, *[refusal] * 20])
        self.assertEqual(summary["by_check"]["execution_success"]["pass_rate"], 1.0)
        self.assertEqual(summary["positive_execution_pass_rate"], 0.0125)
        self.assertEqual(summary["positive_baseline_match_rate"], 0.0125)
        gate = evaluate_quality_gate(
            summary,
            min_pass_rate=0.85,
            min_refusal_pass_rate=0.95,
            min_execution_pass_rate=0.85,
            min_cases=100,
            min_positive_cases=80,
            min_refusal_cases=20,
        )
        self.assertFalse(gate["passed"])
        self.assertIn("positive_execution_pass_rate 0.0125 < 0.8500", gate["failures"])
        self.assertIn("positive_baseline_match_rate 0.0125 < 0.8500", gate["failures"])

    def test_summary_exposes_diagnostic_slices_and_failed_checks(self):
        results = [
            {
                "passed": True,
                "should_refuse": False,
                "category": "aggregation",
                "difficulty": "easy",
                "checks": [{"name": "sql_generated", "passed": True}],
            },
            {
                "passed": False,
                "should_refuse": True,
                "category": "refusal",
                "difficulty": "hard",
                "checks": [{"name": "refusal_expected", "passed": False}],
            },
        ]
        summary = summarize_evaluation(results)
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertEqual(summary["refusal_pass_rate"], 0.0)
        self.assertEqual(summary["by_category"]["aggregation"]["passed"], 1)
        self.assertEqual(summary["failed_checks"], {"refusal_expected": 1})
        self.assertEqual(summary["by_check"]["sql_generated"]["pass_rate"], 1.0)

    def test_quality_gate_reports_each_failed_threshold(self):
        gate = evaluate_quality_gate(
            {"pass_rate": 0.8, "refusal_pass_rate": 0.9},
            min_pass_rate=0.85,
            min_refusal_pass_rate=0.95,
        )
        self.assertFalse(gate["passed"])
        self.assertEqual(len(gate["failures"]), 2)

    def test_quality_gate_rejects_missing_positive_or_refusal_coverage(self):
        gate = evaluate_quality_gate(
            {
                "total": 2,
                "pass_rate": 1.0,
                "positive_total": 2,
                "positive_pass_rate": 1.0,
                "refusal_total": 0,
                "refusal_pass_rate": 1.0,
            },
            min_pass_rate=0.9,
            min_positive_pass_rate=0.9,
            min_refusal_pass_rate=0.9,
            min_cases=3,
            min_positive_cases=1,
            min_refusal_cases=1,
        )
        self.assertFalse(gate["passed"])
        self.assertIn("total 2 < 3", gate["failures"])
        self.assertIn("refusal_total 0 < 1", gate["failures"])

    def test_quality_gate_can_block_semantics_execution_and_retrieval(self):
        gate = evaluate_quality_gate(
            {
                "total": 4,
                "pass_rate": 1.0,
                "positive_total": 3,
                "positive_pass_rate": 1.0,
                "refusal_total": 1,
                "refusal_pass_rate": 1.0,
                "by_check": {
                    "semantic_ir": {"pass_rate": 0.9},
                    "execution_success": {"pass_rate": 0.8},
                    "retrieval_recall": {"pass_rate": 0.7},
                },
            },
            min_pass_rate=0.9,
            min_refusal_pass_rate=0.9,
            min_semantic_ir_pass_rate=1.0,
            min_execution_pass_rate=0.85,
            min_retrieval_recall=0.9,
        )
        self.assertFalse(gate["passed"])
        self.assertEqual(len(gate["failures"]), 5)


if __name__ == "__main__":
    unittest.main()
