import json
from pathlib import Path
import tempfile
import unittest

from src.knowledge.structured import load_knowledge_bundle


SCHEMA = {
    "Fact": {
        "columns": {
            "ID": {"data_type": "int"},
            "Amount": {"data_type": "decimal"},
            "DimID": {"data_type": "int"},
        }
    },
    "Dim": {"columns": {"ID": {"data_type": "int"}, "Name": {"data_type": "nvarchar"}}},
}


class StructuredKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "schema").mkdir()
        (self.root / "domain").mkdir()
        (self.root / "examples").mkdir()

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
        (self.root / "examples" / "gold_sql.jsonl").write_text(
            json.dumps(
                {
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
        self.assertEqual(len(bundle.gold_sql), 1)
        self.assertEqual(bundle.errors, [])
