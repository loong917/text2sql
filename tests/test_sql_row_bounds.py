"""Strict runtime bounds must not redefine independent baseline query meaning."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlglot import exp, parse_one

from tests.catalog_fixture import TEST_CATALOG
from tests.evaluation_fixture import evaluation_cases
from text2sql.domain.sql_validation import SqlSafetyPolicy, limit_tsql_rows
from text2sql.evaluation.wiring import build_evaluation_service

SCHEMA = {"Fact": {"columns": {"ID": "int", "Secret": "nvarchar"}}}


class StrictTsqlRowBoundTests(unittest.TestCase):
    def test_percent_and_ties_fail_closed_instead_of_changing_baseline_truth(self):
        for sql in (
            "SELECT TOP 1 PERCENT ID FROM Fact",
            "SELECT TOP 1 WITH TIES ID FROM Fact ORDER BY ID",
            "SELECT TOP 5000 PERCENT ID FROM Fact",
            "SELECT ID FROM (SELECT TOP 1 PERCENT ID FROM Fact) f",
            "WITH f AS (SELECT TOP 1 WITH TIES ID FROM Fact ORDER BY ID) SELECT ID FROM f",
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "strict row bound"):
                limit_tsql_rows(sql, 3)

    def test_only_literal_nonnegative_top_is_preserved(self):
        for limit in (0, 1, 3):
            with self.subTest(limit=limit):
                limited = limit_tsql_rows(f"SELECT TOP {limit} ID FROM Fact", 3)
                tree = parse_one(limited, read="tsql")
                self.assertEqual(int(tree.args["limit"].expression.this), limit)
        for sql in (
            "SELECT TOP (@amount) ID FROM Fact",
            "SELECT TOP (-1) ID FROM Fact",
            "SELECT TOP (1.5) ID FROM Fact",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                limit_tsql_rows(sql, 3)

    def test_paginated_bounds_preserve_smaller_fetch_and_cap_larger_fetch(self):
        for original, expected in ((1, 1), (3, 3), (5000, 3)):
            with self.subTest(original=original):
                limited = limit_tsql_rows(
                    f"SELECT ID FROM Fact ORDER BY ID OFFSET 10 ROWS "
                    f"FETCH NEXT {original} ROWS ONLY",
                    3,
                )
                tree = parse_one(limited, read="tsql")
                fetch = tree.args["limit"]
                self.assertIsInstance(fetch, exp.Fetch)
                self.assertEqual(int(fetch.args["count"].this), expected)
                self.assertEqual(int(tree.args["offset"].expression.this), 10)

    def test_queries_ctes_and_set_operations_have_a_top_level_strict_bound(self):
        for sql in (
            "SELECT ID FROM Fact",
            "SELECT TOP 5000 ID FROM Fact",
            "WITH f AS (SELECT ID FROM Fact) SELECT ID FROM f",
            "SELECT ID FROM Fact UNION ALL SELECT ID FROM Fact",
        ):
            with self.subTest(sql=sql):
                tree = parse_one(limit_tsql_rows(sql, 3), read="tsql")
                self.assertEqual(int(tree.args["limit"].expression.this), 3)
                self.assertIsNone(tree.args["limit"].args.get("limit_options"))

    def test_nonqueries_batches_and_invalid_bound_inputs_are_not_admitted(self):
        for sql in (
            "",
            "DELETE FROM Fact",
            "SELECT ID INTO Other FROM Fact",
            "SELECT ID FROM Fact; SELECT ID FROM Fact",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                limit_tsql_rows(sql, 3)
        for bound in (0, -1, True, 1.5):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                limit_tsql_rows("SELECT ID FROM Fact", bound)


class BaselineSafetyWiringTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        # Keep this fixture independent of private .env data and runtime clients.
        return SimpleNamespace(
            app_env="test",
            eval_dev_set_path="dev.jsonl",
            eval_test_set_path="test.jsonl",
            schema_cache_ttl_seconds=300,
            sql_max_concurrency=2,
            sql_query_timeout_seconds=4.5,
            max_result_rows=7,
            sql_allowed_schemas=" dbo, reporting, ",
            sql_max_joins=2,
            sql_max_subqueries=1,
            sql_allowed_tables=" Fact, ",
            sql_denied_tables=" Hidden, ",
            sql_denied_columns=" *.Secret, ",
            sql_aggregation_only_tables=" Fact, ",
            sql_allow_select_star=False,
            sql_require_table=True,
            sql_allow_cross_join=False,
        )

    async def build(self, snapshot_schema=SCHEMA):
        executor = AsyncMock()
        snapshot = SimpleNamespace(
            schema=snapshot_schema,
            knowledge=SimpleNamespace(semantic_catalog=Mock(return_value=TEST_CATALOG)),
        )
        with (
            patch("text2sql.evaluation.wiring.VannaSqlExecutor", return_value=executor),
            patch("text2sql.evaluation.wiring.get_live_schema", return_value=SCHEMA),
            patch("text2sql.evaluation.wiring.ArtifactSnapshot.load", return_value=snapshot),
            patch("text2sql.evaluation.wiring.build_text2sql_service", return_value=AsyncMock()),
        ):
            service = await build_evaluation_service(
                self.config(),
                SimpleNamespace(sql_runner=object()),
                knowledge_index_path="fixture/knowledge_index.json",
                knowledge_memory=object(),
            )
        return service, executor

    async def test_wiring_passes_authenticated_schema_row_limit_and_all_policy_controls(self):
        service, _ = await self.build()
        self.assertIs(service._config.live_schema, SCHEMA)
        self.assertEqual(service._config.max_result_rows, 7)
        self.assertEqual(service._config.query_timeout_seconds, 4.5)
        self.assertEqual(
            service._config.safety_policy,
            SqlSafetyPolicy(
                allowed_schemas=("dbo", "reporting"),
                max_joins=2,
                max_subqueries=1,
                allowed_tables=("Fact",),
                denied_tables=("Hidden",),
                denied_columns=("*.Secret",),
                aggregation_only_tables=("Fact",),
                allow_select_star=False,
                require_table=True,
                allow_cross_join=False,
            ),
        )

    async def test_baseline_modifier_cannot_execute_through_composed_service(self):
        service, executor = await self.build()
        for modifier in ("PERCENT", "WITH TIES"):
            with self.subTest(modifier=modifier):
                case = evaluation_cases()[0] | {
                    "baseline_sql": f"SELECT TOP 1 {modifier} COUNT(*) AS Total FROM Fact "
                    "ORDER BY COUNT(*)"
                }
                baseline = await service._baseline(case, {"success": True})
                self.assertFalse(baseline["success"])
                self.assertIn("strict row bound", baseline["error"])
                executor.execute.assert_not_awaited()
