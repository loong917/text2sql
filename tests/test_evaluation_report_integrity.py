"""Offline contract fixtures cannot claim success from altered semantic evidence."""

import unittest
from copy import deepcopy

from pydantic import ValidationError

from tests.evaluation_fixture import evaluation_cases, evaluation_results
from text2sql.evaluation.report_contract import EvaluationResult


class SemanticReportIntegrityTests(unittest.TestCase):
    def result_fixture(self):
        result = evaluation_results(evaluation_cases())[0]
        # Isolate the offline report contract from the live QueryPlan implementation.
        for item in result["checks"]:
            if item["name"].startswith("semantic_ir:"):
                item["actual"] = deepcopy(item["expected"])
                item["passed"] = True
        result["passed"] = all(item["passed"] for item in result["checks"])
        return result

    def semantic_check(self, result, name):
        return next(item for item in result["checks"] if item["name"] == f"semantic_ir:{name}")

    def test_valid_offline_semantic_evidence_passes(self):
        self.assertTrue(EvaluationResult.model_validate(self.result_fixture()).passed)

    def test_changed_actual_cannot_keep_a_passed_verdict(self):
        result = self.result_fixture()
        self.semantic_check(result, "metrics")["actual"] = ["tampered_metric"]
        with self.assertRaisesRegex(ValidationError, "verdict disagrees"):
            EvaluationResult.model_validate(result)

    def test_false_verdict_cannot_hide_equal_evidence(self):
        result = self.result_fixture()
        self.semantic_check(result, "metrics")["passed"] = False
        result["passed"] = False
        with self.assertRaisesRegex(ValidationError, "verdict disagrees"):
            EvaluationResult.model_validate(result)

    def test_bool_cannot_impersonate_an_integer_in_a_passed_snapshot(self):
        result = self.result_fixture()
        shape = self.semantic_check(result, "result_shape")
        shape["expected"] = shape["expected"] | {
            "limit": 1,
            "sort_direction": "desc",
            "sort_metric": "collection_count",
        }
        shape["actual"] = shape["expected"] | {"limit": True}
        self.assertEqual(shape["actual"], shape["expected"])
        with self.assertRaisesRegex(ValidationError, "valid actual snapshot"):
            EvaluationResult.model_validate(result)

    def test_missing_actual_can_be_recorded_as_failed_but_not_passed(self):
        result = self.result_fixture()
        metrics = self.semantic_check(result, "metrics")
        metrics.update(actual=None, passed=False)
        result["passed"] = False
        self.assertFalse(EvaluationResult.model_validate(result).passed)
        metrics["passed"] = True
        with self.assertRaisesRegex(ValidationError, "verdict disagrees"):
            EvaluationResult.model_validate(result)
