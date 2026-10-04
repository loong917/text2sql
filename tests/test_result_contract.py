"""Output labels cannot collapse before query/review/evaluation row conversion."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import pandas as pd

from tests.catalog_fixture import TEST_CATALOG
from tests.evaluation_fixture import evaluation_cases, plan_diagnostics
from tests.test_feedback_service import FeedbackRepository, Provider, SchemaRepository, settings
from text2sql.application.contracts import QueryContext
from text2sql.application.feedback_service import review_feedback
from text2sql.application.query_config import QueryServiceConfig
from text2sql.application.query_execution import execute_query
from text2sql.domain.result_contract import read_tabular_result, validate_column_names
from text2sql.domain.semantic_ir import parse_question_semantics
from text2sql.domain.sql_validation import SqlSafetyPolicy, validate_tsql_ast
from text2sql.evaluation.comparison import (
    ResultComparison,
    compare_rows,
    result_columns,
    result_rows,
)
from text2sql.evaluation.service import EvaluationConfig, EvaluationService, evaluate_case

SCHEMA = {"Fact": {"columns": {"ID": {}, "Amount": {}}}}


class SqlOutputColumnTests(unittest.TestCase):
    def test_final_projection_rejects_casefold_and_literal_alias_collisions(self):
        for sql in (
            "SELECT ID AS X, Amount AS x FROM Fact",
            "SELECT ID, Amount AS id FROM Fact",
            "SELECT ID AS [1], Amount AS '1' FROM Fact",
            "SELECT SUM(Amount), COUNT(*) FROM Fact",
            "SELECT 1, 2 FROM Fact",
            "SELECT ID AS '', Amount AS '' FROM Fact",
            "SELECT ID AS X, Amount AS x FROM Fact UNION ALL SELECT ID, Amount FROM Fact",
        ):
            with self.subTest(sql=sql):
                self.assertIn("输出列名重复", validate_tsql_ast(sql, SCHEMA))

    def test_single_unnamed_expression_and_unique_nonstring_looking_aliases_are_safe(self):
        for sql in (
            "SELECT ID, Amount FROM Fact",
            "SELECT COUNT(*) FROM Fact",
            "SELECT ID AS [1], Amount AS '2' FROM Fact",
            "SELECT ID AS '' FROM Fact",
            "SELECT ID AS X, Amount AS Y FROM Fact UNION ALL SELECT ID, Amount FROM Fact",
        ):
            with self.subTest(sql=sql):
                self.assertIsNone(validate_tsql_ast(sql, SCHEMA))

    def test_allowed_star_expansion_also_rejects_duplicate_labels(self):
        sql = "SELECT *, ID AS id FROM Fact"
        self.assertIn(
            "输出列名重复",
            validate_tsql_ast(sql, SCHEMA, safety_policy=SqlSafetyPolicy(allow_select_star=True)),
        )

    def test_business_projection_renaming_cannot_hide_collision(self):
        from tests.test_semantic_ast import SCHEMA as BUSINESS_SCHEMA

        plan = parse_question_semantics("统计2025年各机构采集量", TEST_CATALOG)
        sql = (
            "SELECT b.InstID AS X,b.OrgName AS x,SUM(a.BCPVolume) AS Volume "
            "FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID=b.InstID "
            "WHERE a.BCDate>='2025-01-01' AND a.BCDate<'2026-01-01' "
            "GROUP BY b.InstID,b.OrgName"
        )
        self.assertIn("输出列名重复", validate_tsql_ast(sql, BUSINESS_SCHEMA, plan))


class TabularContractTests(unittest.TestCase):
    def test_valid_numeric_labels_are_normalized_without_changing_values(self):
        result = read_tabular_result(pd.DataFrame([[1, "002"]], columns=[1, "2"]))
        self.assertEqual(result.columns, ["1", "2"])
        self.assertEqual(result.rows, [{"1": 1, "2": "002"}])
        self.assertEqual(read_tabular_result([{1: "v"}]).rows, [{"1": "v"}])
        self.assertTrue(compare_rows([{1: "v"}], [{"1": "v"}], ["1"], ResultComparison()))

    def test_duplicates_are_rejected_before_dataframe_conversion_even_when_empty(self):
        for columns in (["x", "x"], ["X", "x"], [1, "1"], ["", ""]):
            for values in ([], [[1, 2]]):
                frame = pd.DataFrame(values, columns=columns)
                with self.subTest(columns=columns, empty=not values):
                    with patch.object(frame, "to_dict", side_effect=AssertionError("data loss")):
                        with self.assertRaisesRegex(ValueError, "unique"):
                            read_tabular_result(frame)

    def test_row_objects_require_consistent_unique_stringifiable_keys(self):
        for rows in ([{"X": 1, "x": 2}], [{1: 1, "1": 2}], [{"X": 1}, {"Y": 2}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                read_tabular_result(rows)
        for label in (True, 1.5, None, ("a", "b")):
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_column_names([label])
        for scalar in ("AB", b"AB", bytearray(b"AB"), {"A": "not metadata"}, {"A"}, None, 1):
            with self.subTest(scalar=scalar), self.assertRaises(ValueError):
                validate_column_names(scalar)

    def test_empty_results_preserve_available_metadata_and_single_unnamed_output(self):
        self.assertEqual(read_tabular_result(pd.DataFrame(columns=["Total"])).columns, ["Total"])
        self.assertEqual(read_tabular_result([], declared_columns=["Total"]).columns, ["Total"])
        self.assertEqual(read_tabular_result(pd.DataFrame([[1]], columns=[""])).rows, [{"": 1}])

    def test_comparison_rejects_duplicate_or_inconsistent_column_metadata(self):
        with self.assertRaises(ValueError):
            result_rows(pd.DataFrame([[1, 2]], columns=["X", "x"]))
        with self.assertRaises(ValueError):
            result_columns(pd.DataFrame(columns=[1, "1"]))
        self.assertFalse(compare_rows([{"X": 1}], [{"X": 1}], ["X", "x"], ResultComparison()))
        self.assertFalse(compare_rows([{"X": 1}], [{"X": 1}], ["Y"], ResultComparison()))


class ResultBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_never_captures_or_returns_a_lossy_result(self):
        repository = Mock(capture=AsyncMock())
        response = await execute_query(
            "query",
            "SELECT ID AS X, Amount AS Y FROM Fact",
            QueryContext("prompt", SCHEMA),
            executor=Mock(
                execute=AsyncMock(return_value=pd.DataFrame([[1, 2]], columns=["X", "x"]))
            ),
            repository=repository,
            config=QueryServiceConfig(10, 1, 1),
            attempts=1,
            capture_feedback=True,
            preserve_result_types=True,
        )
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "EXECUTOR_CONTRACT_INVALID")
        repository.capture.assert_not_awaited()

    async def test_positive_feedback_never_promotes_a_lossy_result(self):
        repository = FeedbackRepository()
        executor = Mock(execute=AsyncMock(return_value=pd.DataFrame([[1, 2]], columns=["X", "x"])))
        with (
            patch("text2sql.application.feedback_service.validate_sql", return_value=None),
            self.assertRaisesRegex(ValueError, "Executor 契约"),
        ):
            await review_feedback(
                question="统计事实记录",
                sql="SELECT COUNT(*) AS total FROM Fact",
                validation_label="correct",
                reviewer="admin",
                candidate_tables=["Fact"],
                candidate_score_reasons={},
                config=settings,
                sql_executor=executor,
                feedback_repository=repository,
                schema_repository=SchemaRepository(),
                artifact_provider=Provider(),
            )
        self.assertIsNone(repository.submitted)

    async def test_baseline_cannot_claim_success_after_duplicate_dataframe_labels(self):
        executor = Mock(execute=AsyncMock(return_value=pd.DataFrame([[1, 2]], columns=["X", "x"])))
        service = EvaluationService(
            None,
            executor,
            TEST_CATALOG,
            EvaluationConfig("dev", "test", live_schema=SCHEMA, safety_policy=SqlSafetyPolicy()),
        )
        baseline = await service._baseline(evaluation_cases()[0], {"success": True})
        self.assertFalse(baseline["success"])
        self.assertIn("unique", baseline["error"])

    def test_declared_column_duplicates_cannot_attest_an_evaluation_match(self):
        case = evaluation_cases()[0]
        response = {
            "success": True,
            "outcome": "success",
            "sql": case["baseline_sql"],
            "result": [{"X": 1, "x": 2}],
            "result_columns": ["X", "x"],
            "result_row_count": 1,
            "candidate_tables": ["Fact"],
            "diagnostics": plan_diagnostics(case["question"]),
        }
        baseline = {
            "success": True,
            "columns": ["X", "x"],
            "rows": [{"X": 1, "x": 2}],
            "row_count": 1,
        }
        result = evaluate_case(case, response, TEST_CATALOG, baseline)
        self.assertFalse(result["passed"])
        self.assertFalse(result["baseline_complete"])
