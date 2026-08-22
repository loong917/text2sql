import unittest
from pathlib import Path

from src.domain.semantic_ir import SemanticCatalog, parse_question_semantics
from src.domain.sql_validation import SqlSafetyPolicy, limit_tsql_rows, validate_tsql_ast
from src.evaluation import load_evaluation_cases
from tests.catalog_fixture import TEST_CATALOG

SCHEMA = {
    "Stat_Collection": {
        "columns": {
            name: {} for name in ("BTSID", "BCDate", "BCType", "BCPVolume", "CollectionID")
        },
        "foreign_keys": [],
    },
    "Pub_OrgAddress": {
        "columns": {name: {} for name in ("InstID", "OrgName", "City")},
        "foreign_keys": [],
    },
}


class SemanticAstTests(unittest.TestCase):
    def setUp(self):
        self.question = "统计杭州市各机构2025年的成分血采集量"
        self.ir = parse_question_semantics(self.question, TEST_CATALOG)
        self.good_sql = (
            "SELECT b.InstID, b.OrgName, SUM(a.BCPVolume) AS Volume "
            "FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID "
            "WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' "
            "AND a.BCType = '1' AND b.City = '杭州市' "
            "GROUP BY b.InstID, b.OrgName"
        )

    def test_semantic_ir_normalizes_entities_and_granularity(self):
        self.assertEqual(self.ir.entity_filters["city"], ("杭州市",))
        self.assertEqual(self.ir.entity_filters["blood_type"], ("1",))
        self.assertEqual(self.ir.date_start, "2025-01-01")
        self.assertEqual(self.ir.expected_granularity, "one_row_per_institution")

    def test_generic_city_dimension_is_not_treated_as_city_filter(self):
        ir = parse_question_semantics("统计2023年每个城市的成分血采集量", TEST_CATALOG)
        self.assertIn("city", ir.dimensions)
        self.assertNotIn("city", ir.entity_filters)

    def test_valid_query_passes(self):
        self.assertIsNone(validate_tsql_ast(self.good_sql, SCHEMA, self.ir))

    def test_missing_blood_type_is_rejected(self):
        sql = self.good_sql.replace(" AND a.BCType = '1'", "")
        self.assertIn("BCType", validate_tsql_ast(sql, SCHEMA, self.ir))

    def test_noncanonical_city_is_rejected(self):
        sql = self.good_sql.replace("杭州市", "杭州")
        self.assertIn("city", validate_tsql_ast(sql, SCHEMA, self.ir))

    def test_missing_date_range_is_rejected(self):
        sql = self.good_sql.replace("a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND ", "")
        self.assertIn("日期范围", validate_tsql_ast(sql, SCHEMA, self.ir))

    def test_write_statement_is_rejected(self):
        error = validate_tsql_ast("DELETE FROM Stat_Collection", SCHEMA, self.ir)
        self.assertIn("写操作", error)

    def test_select_into_is_rejected(self):
        error = validate_tsql_ast("SELECT * INTO copied_data FROM Stat_Collection", SCHEMA)
        self.assertIn("写操作", error)

    def test_cross_database_access_is_rejected(self):
        error = validate_tsql_ast("SELECT COUNT(*) FROM OtherDb.dbo.Stat_Collection", SCHEMA)
        self.assertIn("跨数据库", error)

    def test_unauthorized_schema_is_rejected(self):
        error = validate_tsql_ast(
            "SELECT COUNT(*) FROM audit.Stat_Collection",
            SCHEMA,
            safety_policy=SqlSafetyPolicy(allowed_schemas=("dbo",)),
        )
        self.assertIn("未授权 Schema", error)

    def test_ambiguous_unqualified_column_is_rejected(self):
        schema = {
            **SCHEMA,
            "OtherTable": {"columns": {"InstID": {}}, "foreign_keys": []},
        }
        error = validate_tsql_ast(
            "SELECT InstID FROM Pub_OrgAddress JOIN OtherTable ON 1=1", schema
        )
        self.assertIn("歧义", error)

    def test_semantics_can_be_driven_by_external_catalog(self):
        catalog = SemanticCatalog(
            metrics=(
                {
                    "id": "custom_metric",
                    "aliases": ["自定义指标"],
                    "aggregation": "SUM",
                    "column": "BCPVolume",
                    "source_table": "Stat_Collection",
                },
            )
        )
        ir = parse_question_semantics("统计自定义指标", catalog)
        self.assertEqual(ir.metrics[0].name, "custom_metric")
        self.assertEqual(ir.required_tables, ("Stat_Collection",))

    def test_execution_limit_is_applied_before_query(self):
        self.assertIn("TOP 101", limit_tsql_rows("SELECT * FROM Stat_Collection", 101))
        self.assertIn("TOP 101", limit_tsql_rows("SELECT TOP 5000 * FROM Stat_Collection", 101))
        self.assertIn("TOP 10", limit_tsql_rows("SELECT TOP 10 * FROM Stat_Collection", 101))

    def test_data_access_policy_restricts_tables_columns_and_raw_detail(self):
        policy = SqlSafetyPolicy(
            allowed_tables=("Stat_Collection",),
            denied_columns=("Stat_Collection.BCPVolume",),
            aggregation_only_tables=("Stat_Collection",),
        )
        self.assertIn(
            "未授权表",
            validate_tsql_ast("SELECT City FROM Pub_OrgAddress", SCHEMA, safety_policy=policy),
        )
        self.assertIn(
            "禁止访问字段",
            validate_tsql_ast(
                "SELECT SUM(BCPVolume) FROM Stat_Collection", SCHEMA, safety_policy=policy
            ),
        )
        self.assertIn(
            "只能用于聚合查询",
            validate_tsql_ast("SELECT BCType FROM Stat_Collection", SCHEMA, safety_policy=policy),
        )
        self.assertIn(
            "SELECT *",
            validate_tsql_ast("SELECT * FROM Stat_Collection", SCHEMA, safety_policy=policy),
        )

    def test_count_star_is_allowed_when_direct_star_projection_is_denied(self):
        self.assertIsNone(
            validate_tsql_ast(
                "SELECT COUNT(*) AS total FROM Stat_Collection",
                SCHEMA,
                safety_policy=SqlSafetyPolicy(
                    allowed_tables=("Stat_Collection",),
                    aggregation_only_tables=("Stat_Collection",),
                ),
            )
        )

    def test_union_requires_filter_in_every_fact_branch(self):
        ir = parse_question_semantics("统计2025年全血与成分血采集人次", TEST_CATALOG)
        sql = (
            "SELECT COUNT(*) AS Times FROM Stat_Collection "
            "WHERE BCDate >= '2025-01-01' AND BCDate < '2026-01-01' AND BCType='0' "
            "UNION ALL SELECT COUNT(*) AS Times FROM Stat_Collection "
            "WHERE BCDate >= '2025-01-01' AND BCDate < '2026-01-01'"
        )
        self.assertIn("查询分支", validate_tsql_ast(sql, SCHEMA, ir))

    def test_top_n_sort_and_distinct_are_explicit_semantic_constraints(self):
        ir = parse_question_semantics("查询采集量最高的前5个不重复机构", TEST_CATALOG)
        self.assertEqual(ir.limit, 5)
        self.assertEqual(ir.sort_direction, "desc")
        self.assertTrue(ir.distinct)
        valid = (
            "SELECT DISTINCT TOP 5 b.InstID, b.OrgName, SUM(a.BCPVolume) AS Volume "
            "FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID=b.InstID "
            "GROUP BY b.InstID, b.OrgName ORDER BY Volume DESC"
        )
        self.assertIsNone(validate_tsql_ast(valid, SCHEMA, ir))
        self.assertIn(
            "前 5 条",
            validate_tsql_ast(valid.replace("TOP 5 ", ""), SCHEMA, ir),
        )

    def test_cross_join_and_join_without_on_are_rejected(self):
        policy = SqlSafetyPolicy(allow_cross_join=False)
        self.assertIn(
            "笛卡尔积",
            validate_tsql_ast(
                "SELECT COUNT(*) FROM Stat_Collection CROSS JOIN Pub_OrgAddress",
                SCHEMA,
                safety_policy=policy,
            ),
        )
        self.assertIn(
            "笛卡尔积",
            validate_tsql_ast(
                "SELECT COUNT(*) FROM Stat_Collection JOIN Pub_OrgAddress",
                SCHEMA,
                safety_policy=policy,
            ),
        )

    def test_metric_must_bind_to_the_catalog_column(self):
        ir = parse_question_semantics("统计2025年的成分血采集总量", TEST_CATALOG)
        sql = (
            "SELECT SUM(a.CollectionID) AS Volume FROM Stat_Collection a "
            "WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' "
            "AND a.BCType='1'"
        )
        self.assertIn("BCPVolume", validate_tsql_ast(sql, SCHEMA, ir))

    def test_repository_baselines_pass_ast_and_semantic_validation(self):
        root = Path(__file__).resolve().parents[1] / "evaluation"
        cases = [
            case.payload
            for split in ("retrieval_train", "dev", "test")
            for case in load_evaluation_cases(root / f"{split}.jsonl", expected_split=split)
        ]
        failures = []
        for case in cases:
            sql = str(case.get("baseline_sql") or "").strip()
            if not sql:
                continue
            error = validate_tsql_ast(
                sql, SCHEMA, parse_question_semantics(case["question"], TEST_CATALOG)
            )
            if error:
                failures.append((case["question"], error))
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
