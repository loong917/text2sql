import unittest
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pandas as pd

from tests.catalog_fixture import TEST_CATALOG
from tests.evaluation_fixture import EVALUATION_SCHEMA, evaluation_cases, plan_diagnostics
from text2sql.domain.sql_validation import SqlSafetyPolicy
from text2sql.evaluation.comparison import ResultComparison, compare_rows, values_equal
from text2sql.evaluation.service import EvaluationConfig, EvaluationService, evaluate_case
from text2sql.evaluation.sql_assertions import SqlAssertions


class ResultComparisonTests(unittest.TestCase):
    def test_null_types_and_precise_numbers_are_not_stringified(self):
        options = ResultComparison()
        self.assertFalse(values_equal(None, "", options))
        self.assertFalse(values_equal(1, "1", options))
        self.assertFalse(values_equal(True, 1, options))
        self.assertFalse(values_equal(datetime(2025, 1, 1), "2025-01-01T00:00:00", options))
        self.assertTrue(values_equal(None, pd.NA, options))
        self.assertTrue(values_equal(Decimal("1.00"), 1, options))
        self.assertFalse(values_equal(Decimal("9007199254740993"), 9007199254740992.0, options))

    def test_bag_preserves_duplicates_and_ordered_preserves_sequence(self):
        expected = [{"N": 1}, {"N": 1}, {"N": 2}]
        reversed_rows = list(reversed(expected))
        self.assertTrue(compare_rows(reversed_rows, expected, ["N"], ResultComparison("bag")))
        self.assertFalse(compare_rows(reversed_rows, expected, ["N"], ResultComparison("ordered")))
        self.assertFalse(
            compare_rows([{"N": 1}, {"N": 2}, {"N": 2}], expected, ["N"], ResultComparison())
        )

    def test_numeric_tolerance_is_explicit_and_bag_matching_is_not_greedy(self):
        options = ResultComparison.from_case({"result_comparison": {"absolute_tolerance": "0.1"}})
        self.assertTrue(compare_rows([{"N": Decimal("1.09")}], [{"N": 1}], ["N"], options))
        self.assertFalse(compare_rows([{"N": Decimal("1.11")}], [{"N": 1}], ["N"], options))
        overlapping = ResultComparison(absolute_tolerance=Decimal("0.6"))
        self.assertTrue(
            compare_rows([{"N": 0.5}, {"N": 0.0}], [{"N": 0.0}, {"N": 1.0}], ["N"], overlapping)
        )

    def test_order_by_defaults_to_ordered_and_invalid_tolerance_is_rejected(self):
        self.assertEqual(
            ResultComparison.from_case({"baseline_sql": "SELECT N FROM Fact ORDER BY N"}).mode,
            "ordered",
        )
        for value in (-1, "NaN", "Infinity", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ResultComparison.from_case({"result_comparison": {"relative_tolerance": value}})


class SqlAssertionTests(unittest.TestCase):
    def test_comments_and_literals_do_not_satisfy_structure(self):
        assertions = SqlAssertions(
            "SELECT 'Secret GROUP BY Fact.Value' AS label FROM Other -- JOIN Fact ON Fact.ID=Other.ID"
        )
        self.assertFalse(assertions.table("Fact"))
        self.assertFalse(assertions.column("Value"))
        self.assertFalse(assertions.forbidden("Secret"))
        self.assertFalse(assertions.checks({"must_have_group_by": True})[0]["passed"])

    def test_filters_must_be_guaranteed_conjuncts_and_join_endpoints_resolve_aliases(self):
        valid = SqlAssertions(
            "SELECT a.ID FROM Fact a JOIN Dimension b ON b.ID=a.DimID WHERE a.Kind='1' AND a.Year>=2024"
        )
        self.assertTrue(valid.predicate("Kind = '1'"))
        self.assertTrue(valid.predicate("Fact.DimID = Dimension.ID", join=True))
        self.assertFalse(valid.predicate("Fact.ID = Dimension.DimID", join=True))
        bypass = SqlAssertions("SELECT Kind FROM Fact WHERE Kind='1' OR 1=1")
        self.assertFalse(bypass.predicate("Kind = '1'"))
        unicode_literal = SqlAssertions(
            "SELECT a.ID FROM dbo.Fact a JOIN dbo.Dimension b ON b.ID=a.DimID WHERE a.City=N'杭州'"
        )
        self.assertTrue(unicode_literal.predicate("City = '杭州'"))
        self.assertTrue(unicode_literal.predicate("Fact.DimID = Dimension.ID", join=True))


class EvaluationOutcomeTests(unittest.TestCase):
    def positive_response(self, case):
        return {
            "success": True,
            "outcome": "success",
            "sql": case["baseline_sql"],
            "result": [{"Total": 1}],
            "candidate_tables": ["Fact"],
            "result_columns": ["Total"],
            "result_row_count": 1,
            "diagnostics": plan_diagnostics(case["question"]),
        }

    def semantic_checks(self, result):
        return [check for check in result["checks"] if check["name"].startswith("semantic_ir:")]

    def test_actual_wrong_plan_is_not_replaced_by_a_correct_independent_parse(self):
        case = evaluation_cases()[0]
        response = self.positive_response(case)
        response["diagnostics"]["semantic_ir"]["metrics"][0]["name"] = "wrong_metric"
        baseline = {"success": True, "rows": [{"Total": 1}], "columns": ["Total"], "row_count": 1}
        result = evaluate_case(case, response, TEST_CATALOG, baseline)
        self.assertFalse(result["passed"])
        metric_check = next(
            check
            for check in self.semantic_checks(result)
            if check["name"] == "semantic_ir:metrics"
        )
        self.assertIn("frozen definition", metric_check["actual"]["evidence_error"])
        self.assertFalse(metric_check["passed"])

    def test_same_metric_id_cannot_hide_changed_physical_definition(self):
        case = evaluation_cases()[0]
        mutations = (
            {"aggregate": "SUM"},
            {"source_table": "OtherFact"},
            {"column": "WrongColumn"},
            {"output_alias": "WrongLabel"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                response = self.positive_response(case)
                metric = response["diagnostics"]["semantic_ir"]["metrics"][0]
                metric.update(mutation)
                result = evaluate_case(case, response, TEST_CATALOG)
                semantic = self.semantic_checks(result)
                self.assertTrue(all(not check["passed"] for check in semantic))
                self.assertIn("differs from catalog", semantic[0]["actual"]["evidence_error"])

    def test_missing_malformed_or_wrong_question_plan_fails_every_semantic_check(self):
        case = evaluation_cases()[0]
        good = self.positive_response(case)
        malformed = deepcopy(good["diagnostics"])
        malformed["semantic_ir"]["limit"] = "5"
        missing_field = deepcopy(good["diagnostics"])
        del missing_field["semantic_ir"]["metrics"]
        wrong_question = deepcopy(good["diagnostics"])
        wrong_question["semantic_ir"]["original_question"] = "another question"
        for diagnostics in (
            None,
            {},
            {"semantic_ir": {}},
            malformed,
            missing_field,
            wrong_question,
        ):
            with self.subTest(diagnostics=diagnostics):
                response = good | {"diagnostics": diagnostics}
                result = evaluate_case(case, response, TEST_CATALOG)
                semantic = self.semantic_checks(result)
                self.assertEqual(len(semantic), len(case["expected_semantic_ir"]))
                self.assertTrue(all(not check["passed"] for check in semantic))

    def test_actual_plan_is_sufficient_without_any_parser_fallback(self):
        case = evaluation_cases()[0]
        response = self.positive_response(case)
        with patch(
            "text2sql.domain.semantic_ir.parse_question_semantics",
            side_effect=AssertionError("evaluation must not parse the question again"),
        ):
            result = evaluate_case(case, response, TEST_CATALOG)
        self.assertTrue(all(check["passed"] for check in self.semantic_checks(result)))

    def test_refusal_requires_explicit_reason_and_correct_outcome(self):
        case = evaluation_cases()[1]
        for outcome in (
            "infrastructure_error",
            "validation_failed",
            "generation_failed",
            "clarification_required",
        ):
            with self.subTest(outcome=outcome):
                result = evaluate_case(
                    case,
                    {
                        "success": False,
                        "outcome": outcome,
                        "refusal_reason": "失败原因",
                        "error": "timeout",
                    },
                    TEST_CATALOG,
                )
                self.assertFalse(result["passed"])
                self.assertEqual(result["outcome"], outcome)
        self.assertFalse(
            evaluate_case(case, {"success": False, "outcome": "refused"}, TEST_CATALOG)["passed"]
        )
        self.assertTrue(
            evaluate_case(
                case,
                {
                    "success": False,
                    "outcome": "refused",
                    "refusal_reason": "超出范围",
                    "diagnostics": plan_diagnostics(case["question"]),
                },
                TEST_CATALOG,
            )["passed"]
        )
        clarification = case | {"expected_outcome": "clarification_required"}
        self.assertTrue(
            evaluate_case(
                clarification,
                {
                    "success": False,
                    "outcome": "clarification_required",
                    "refusal_reason": "缺少时间范围",
                    "diagnostics": plan_diagnostics(case["question"]),
                },
                TEST_CATALOG,
            )["passed"]
        )

    def test_matching_truncated_rows_do_not_attest_complete_correctness(self):
        case = evaluation_cases()[0]
        response = {
            "success": True,
            "outcome": "success",
            "sql": case["baseline_sql"],
            "result": [{"Total": 1}],
            "candidate_tables": ["Fact"],
            "result_columns": ["Total"],
            "result_row_count": 1,
            "result_truncated": True,
            "diagnostics": plan_diagnostics(case["question"]),
        }
        baseline = {"success": True, "rows": [{"Total": 1}], "columns": ["Total"], "row_count": 1}
        result = evaluate_case(case, response, TEST_CATALOG, baseline)
        self.assertFalse(result["passed"])
        self.assertFalse(result["baseline_complete"])
        self.assertFalse(
            next(
                item["passed"]
                for item in result["checks"]
                if item["name"] == "baseline_result_match"
            )
        )


class EmptyResultMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluation_cleanup_closes_executor_when_query_cleanup_fails(self):
        class Query:
            async def aclose(self):
                raise RuntimeError("query cleanup failed")

        class Executor:
            closed = False

            async def aclose(self):
                self.closed = True

        executor = Executor()
        service = EvaluationService(
            Query(), executor, TEST_CATALOG, EvaluationConfig("dev", "test")
        )
        with self.assertRaisesRegex(RuntimeError, "query cleanup failed"):
            await service.aclose()
        self.assertTrue(executor.closed)

    async def test_empty_dataframe_baseline_retains_column_metadata(self):
        class Executor:
            async def execute(self, sql, *, timeout_seconds):
                return pd.DataFrame(columns=["Total"])

        service = EvaluationService(
            None,
            Executor(),
            TEST_CATALOG,
            EvaluationConfig(
                "dev",
                "test",
                live_schema=EVALUATION_SCHEMA,
                safety_policy=SqlSafetyPolicy(),
            ),
        )
        baseline = await service._baseline(evaluation_cases()[0], {"success": True})
        self.assertEqual(baseline["columns"], ["Total"])
        self.assertEqual(baseline["row_count"], 0)
