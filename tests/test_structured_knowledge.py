import json
import tempfile
import unittest
from pathlib import Path

from tests.evaluation_fixture import reviewed_evaluation_case
from tests.schema_fixture import synthetic_schema
from text2sql.knowledge.governance import content_digest
from text2sql.knowledge.structured import load_knowledge_bundle

SCHEMA = synthetic_schema(
    {
        "Fact": {
            "columns": {
                "ID": {"data_type": "int"},
                "Amount": {"data_type": "decimal"},
                "DimID": {"data_type": "int"},
            }
        },
        "Dim": {
            "columns": {"ID": {"data_type": "int"}, "Name": {"data_type": "nvarchar"}},
            "unique_keys": {"UQ_test_dimension": ["ID"]},
        },
    }
)

GOLD_TRUTH = {
    "version": 3,
    "metrics": ["amount_sum"],
    "metric_predicates": [],
    "dimensions": [],
    "entity_filters": {},
    "entity_filter_intents": [],
    "date_range": {"start": None, "end": None},
    "granularity": "aggregate",
    "required_tables": ["Fact"],
    "result_shape": {"sort_direction": None, "sort_metric": None, "limit": None, "distinct": False},
    "time_granularity": None,
    "comparison": None,
    "ambiguities": [],
    "status": "ready",
    "date_is_relative": False,
    "reference_date": None,
}


def reviewed_gold():
    sql = "SELECT SUM(Amount) AS Value FROM Fact"
    evidence = reviewed_evaluation_case(
        {"baseline_sql": sql, "expected_result_columns": ["Value"]}, schema=SCHEMA
    )
    gold = {
        "id": "gold-sum",
        "question": "sum",
        "sql": sql,
        "tables": ["Fact"],
        "status": "approved",
        "semantic_ir": GOLD_TRUTH,
        "review": evidence["review"],
        "execution": evidence["execution"],
    }
    gold["review"]["content_sha256"] = content_digest(gold)
    return gold


class StructuredKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "schema").mkdir()
        (self.root / "domain").mkdir()
        (self.root / "examples").mkdir()
        (self.root / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "name": "test-knowledge"}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_unknown_metric_column_and_unapproved_gold(self):
        (self.root / "domain" / "metrics.json").write_text(
            json.dumps(
                [
                    {
                        "id": "bad",
                        "name": "bad",
                        "source_table": "Fact",
                        "aggregation": "SUM",
                        "column": "Missing",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (self.root / "examples" / "gold_sql.jsonl").write_text(
            json.dumps(
                {
                    "question": "q",
                    "sql": "SELECT Amount FROM Fact",
                    "tables": ["Fact"],
                    "status": "candidate",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        bundle = load_knowledge_bundle(self.root, SCHEMA)
        self.assertEqual(bundle.metrics, [])
        self.assertEqual(bundle.gold_sql, [])
        self.assertTrue(any("unknown column" in item for item in bundle.errors))

    def test_accepts_approved_ast_checked_gold(self):
        (self.root / "domain" / "metrics.json").write_text(
            json.dumps(
                [
                    {
                        "id": "amount_sum",
                        "name": "sum",
                        "source_table": "Fact",
                        "aggregation": "SUM",
                        "column": "Amount",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (self.root / "examples" / "gold_sql.jsonl").write_text(
            json.dumps(reviewed_gold()) + "\n",
            encoding="utf-8",
        )
        bundle = load_knowledge_bundle(self.root, SCHEMA)
        self.assertEqual(len(bundle.gold_sql), 1)
        self.assertEqual(bundle.errors, [])

    def test_approval_does_not_replace_a_grounded_metric_definition(self):
        (self.root / "examples" / "gold_sql.jsonl").write_text(
            json.dumps(
                {
                    "id": "ungrounded-sum",
                    "question": "sum",
                    "sql": "SELECT SUM(Amount) FROM Fact",
                    "tables": ["Fact"],
                    "status": "approved",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        bundle = load_knowledge_bundle(self.root, SCHEMA)
        self.assertEqual(bundle.gold_sql, [])
        self.assertTrue(bundle.errors)
