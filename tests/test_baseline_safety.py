"""Baseline ground truth is independently admitted and bounded before execution."""

import json
import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd

from tests.catalog_fixture import TEST_CATALOG
from tests.evaluation_fixture import evaluation_cases, plan_diagnostics, reviewed_evaluation_case
from text2sql.domain.sql_validation import SqlSafetyPolicy, limit_tsql_rows
from text2sql.evaluation.dataset import load_evaluation_cases_bytes, sql_template_fingerprint
from text2sql.evaluation.service import EvaluationConfig, EvaluationService, evaluate_case

SCHEMA = {
    "Fact": {"columns": {"ID": "int", "Kind": "nvarchar", "Secret": "nvarchar"}},
    "Other": {"columns": {"ID": "int"}},
}


class BaselineDatasetSafetyTests(unittest.TestCase):
    def load(self, case):
        return load_evaluation_cases_bytes(
            (json.dumps(case) + "\n").encode(), source="test.jsonl", expected_split="test"
        )

    def test_fingerprint_rejects_writes_batches_and_nonquery_commands(self):
        for sql in (
            "DELETE FROM Fact",
            "UPDATE Fact SET Kind='1'",
            "INSERT INTO Fact(ID) VALUES (1)",
            "DROP TABLE Fact",
            "EXEC dbo.unsafe_procedure",
            "SELECT ID INTO CopiedFact FROM Fact",
            "SELECT ID FROM Fact; DELETE FROM Fact",
            "SELECT ID FROM Fact; SELECT ID FROM Other",
            "",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                sql_template_fingerprint(sql)

    def test_fingerprint_accepts_single_readonly_cte_and_set_query(self):
        for sql in (
            "SELECT ID FROM Fact -- a harmless comment",
            "WITH f AS (SELECT ID FROM Fact) SELECT ID FROM f",
            "SELECT ID FROM Fact UNION ALL SELECT ID FROM Other",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(len(sql_template_fingerprint(sql)), 64)

    def test_positive_test_cases_cannot_disable_or_omit_execution(self):
        case = evaluation_cases()[0]
        for value in (None, False, "true", 1):
            candidate = case.copy()
            if value is None:
                candidate.pop("must_execute")
            else:
                candidate["must_execute"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(candidate)
        self.assertEqual(self.load(case)[0].payload["must_execute"], True)

    def test_dev_cases_may_remain_generation_only(self):
        case = evaluation_cases()[0] | {"split": "dev", "must_execute": False}
        loaded = load_evaluation_cases_bytes(
            json.dumps(reviewed_evaluation_case(case)).encode(),
            source="dev.jsonl",
            expected_split="dev",
        )
        self.assertFalse(loaded[0].payload["must_execute"])

    def test_dataset_rejects_baseline_contradicting_declared_constraints(self):
        case = evaluation_cases()[0] | {"must_include_filters": ["Kind = '1'"]}
        with self.assertRaisesRegex(ValueError, "declared case constraints"):
            self.load(case)

    def test_dataset_rejects_nonstring_baseline(self):
        with self.assertRaisesRegex(ValueError, "baseline_sql must be a string"):
            self.load(evaluation_cases()[0] | {"baseline_sql": ["SELECT ID FROM Fact"]})


class BaselineExecutionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def service(self, executor, **overrides):
        config = {
            "live_schema": SCHEMA,
            "safety_policy": SqlSafetyPolicy(allowed_tables=("Fact",)),
            "max_result_rows": 2,
        } | overrides
        return EvaluationService(
            None, executor, TEST_CATALOG, EvaluationConfig("dev", "test", **config)
        )

    async def test_unsafe_or_unauthorized_baseline_never_reaches_executor(self):
        for sql in (
            "DELETE FROM Fact",
            "SELECT ID FROM Fact; DELETE FROM Fact",
            "SELECT ID INTO CopiedFact FROM Fact",
            "EXEC dbo.unsafe_procedure",
            "SELECT ID FROM private.Fact",
            "SELECT ID FROM Other",
            "SELECT ID FROM Missing",
            "SELECT MissingColumn FROM Fact",
            "SELECT * FROM Fact",
            "SELECT ID FROM other_database.dbo.Fact",
        ):
            with self.subTest(sql=sql):
                executor = AsyncMock()
                baseline = await self.service(executor)._baseline(
                    evaluation_cases()[0] | {"baseline_sql": sql}, {"success": True}
                )
                self.assertFalse(baseline["success"])
                self.assertIn("admission failed", baseline["error"])
                executor.execute.assert_not_awaited()

    async def test_absent_schema_or_policy_fails_closed(self):
        for inputs in ({"live_schema": None}, {"live_schema": {}}, {"safety_policy": None}):
            with self.subTest(inputs=inputs):
                executor = AsyncMock()
                baseline = await self.service(executor, **inputs)._baseline(
                    evaluation_cases()[0], {"success": True}
                )
                self.assertFalse(baseline["success"])
                self.assertIn("authorized schema and safety policy", baseline["error"])
                executor.execute.assert_not_awaited()

    async def test_denied_column_is_checked_before_execution(self):
        executor = AsyncMock()
        service = self.service(
            executor, safety_policy=SqlSafetyPolicy(denied_columns=("Fact.Secret",))
        )
        baseline = await service._baseline(
            evaluation_cases()[0] | {"baseline_sql": "SELECT Secret FROM Fact"},
            {"success": True},
        )
        self.assertFalse(baseline["success"])
        executor.execute.assert_not_awaited()

    async def test_case_constraints_are_not_inferred_from_model_response(self):
        executor = AsyncMock()
        case = evaluation_cases()[0] | {"must_include_filters": ["Kind = '1'"]}
        with patch(
            "text2sql.domain.semantic_ir.parse_question_semantics",
            side_effect=AssertionError("baseline must not parse model semantics"),
        ):
            baseline = await self.service(executor)._baseline(case, {"success": True})
        self.assertFalse(baseline["success"])
        self.assertIn("declared case constraints", baseline["error"])
        executor.execute.assert_not_awaited()

    async def test_max_plus_one_baseline_is_trimmed_and_marked_incomplete(self):
        executor = AsyncMock()
        executor.execute.return_value = pd.DataFrame({"Total": [1, 2, 3]})
        case = evaluation_cases()[0]
        baseline = await self.service(executor)._baseline(case, {"success": True})
        executor.execute.assert_awaited_once_with(
            limit_tsql_rows(case["baseline_sql"], 3), timeout_seconds=30.0
        )
        self.assertTrue(baseline["success"])
        self.assertTrue(baseline["truncated"])
        self.assertEqual(baseline["rows"], [{"Total": 1}, {"Total": 2}])
        self.assertEqual(baseline["row_count"], 2)
        result = evaluate_case(
            case,
            {
                "success": True,
                "outcome": "success",
                "sql": case["baseline_sql"],
                "result": baseline["rows"],
                "result_columns": ["Total"],
                "result_row_count": 2,
                "candidate_tables": ["Fact"],
                "diagnostics": plan_diagnostics(case["question"]),
            },
            TEST_CATALOG,
            baseline,
        )
        self.assertFalse(result["baseline_complete"])
        self.assertFalse(result["passed"])

    async def test_exact_limit_and_empty_results_preserve_complete_metadata(self):
        for values in ([], [1, 2]):
            with self.subTest(values=values):
                executor = AsyncMock()
                executor.execute.return_value = pd.DataFrame({"Total": values})
                baseline = await self.service(executor)._baseline(
                    evaluation_cases()[0], {"success": True}
                )
                self.assertTrue(baseline["success"])
                self.assertFalse(baseline["truncated"])
                self.assertEqual(baseline["columns"], ["Total"])
                self.assertEqual(baseline["row_count"], len(values))

    def test_invalid_row_limits_are_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive integer"):
                EvaluationConfig("dev", "test", max_result_rows=value)
