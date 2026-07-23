import unittest
import json
from pathlib import Path

from src.services.ast_validator import validate_tsql_ast
from src.services.semantic_ir import parse_question_semantics


SCHEMA = {
    "Stat_Collection": {
        "columns": {
            name: {}
            for name in ("BTSID", "BCDate", "BCType", "BCPVolume", "CollectionID")
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
        self.ir = parse_question_semantics(self.question)
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

    def test_valid_query_passes(self):
        self.assertIsNone(validate_tsql_ast(self.good_sql, SCHEMA, self.ir))

    def test_missing_blood_type_is_rejected(self):
        sql = self.good_sql.replace(" AND a.BCType = '1'", "")
        self.assertIn("BCType", validate_tsql_ast(sql, SCHEMA, self.ir))

    def test_noncanonical_city_is_rejected(self):
        sql = self.good_sql.replace("杭州市", "杭州")
        self.assertIn("城市", validate_tsql_ast(sql, SCHEMA, self.ir))

    def test_missing_date_range_is_rejected(self):
        sql = self.good_sql.replace(
            "a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND ", ""
        )
        self.assertIn("日期范围", validate_tsql_ast(sql, SCHEMA, self.ir))

    def test_write_statement_is_rejected(self):
        error = validate_tsql_ast("DELETE FROM Stat_Collection", SCHEMA, self.ir)
        self.assertIn("写操作", error)

    def test_union_requires_filter_in_every_fact_branch(self):
        ir = parse_question_semantics("统计2025年全血与成分血采集人次")
        sql = (
            "SELECT COUNT(*) AS Times FROM Stat_Collection "
            "WHERE BCDate >= '2025-01-01' AND BCDate < '2026-01-01' AND BCType='0' "
            "UNION ALL SELECT COUNT(*) AS Times FROM Stat_Collection "
            "WHERE BCDate >= '2025-01-01' AND BCDate < '2026-01-01'"
        )
        self.assertIn("查询分支", validate_tsql_ast(sql, SCHEMA, ir))

    def test_repository_baselines_pass_ast_and_semantic_validation(self):
        cases = json.loads(
            (Path(__file__).resolve().parents[1] / "EVAL_SET.json").read_text(
                encoding="utf-8"
            )
        )
        failures = []
        for case in cases:
            sql = str(case.get("baseline_sql") or "").strip()
            if not sql:
                continue
            error = validate_tsql_ast(
                sql, SCHEMA, parse_question_semantics(case["question"])
            )
            if error:
                failures.append((case["question"], error))
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
