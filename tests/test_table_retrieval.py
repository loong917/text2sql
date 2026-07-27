import tempfile
from pathlib import Path
import unittest

from src.core.config import settings
from src.retrieval.calibrator import PlattCalibrator
from src.retrieval.dataset import extract_sql_tables, serialize_pair_records, RetrievalExample
from src.retrieval.schema_graph import bridge_tables
from src.retrieval.table_card import build_table_cards, load_schema_snapshot
from src.retrieval.table_retriever import TableRetriever
from src.domain.semantic_ir import parse_question_semantics


SCHEMA = {
    "Fact": {
        "description": "事实表",
        "columns": {"DimID": {"data_type": "int", "description": "维度编号"}},
        "foreign_keys": [{"column_name": "DimID", "referenced_table": "Bridge"}],
    },
    "Bridge": {
        "description": "桥接表",
        "columns": {"DimID": {"data_type": "int", "description": ""}},
        "foreign_keys": [{"column_name": "DimID", "referenced_table": "Dimension"}],
    },
    "Dimension": {
        "description": "维度表",
        "columns": {"Name": {"data_type": "nvarchar", "description": "名称"}},
        "foreign_keys": [],
    },
}


class FakeEmbedder:
    async def embed(self, texts):
        vectors = []
        for text in texts:
            if "表名: Bridge" in text:
                vectors.append([0.0, 1.0])
            elif "Fact" in text or "事实" in text:
                vectors.append([1.0, 0.0])
            elif "Dimension" in text or "维度" in text:
                vectors.append([0.8, 0.2])
            else:
                vectors.append([1.0, 0.0])
        return vectors


class TableRetrievalTests(unittest.IsolatedAsyncioTestCase):
    def test_table_cards_are_one_document_per_table(self):
        cards = build_table_cards(SCHEMA)
        self.assertEqual({card.table_name for card in cards}, set(SCHEMA))
        self.assertTrue(all(card.token_cost > 0 for card in cards))

    def test_ast_labels_tables(self):
        tables = extract_sql_tables("SELECT * FROM Fact f JOIN Dimension d ON 1=1")
        self.assertEqual(tables, ("Fact", "Dimension"))
        records = serialize_pair_records(
            [RetrievalExample("问题", tables, "test")], SCHEMA
        )
        labels = {item["table_name"]: item["label"] for item in records}
        self.assertEqual(labels, {"Fact": 1, "Bridge": 0, "Dimension": 1})

    def test_schema_graph_adds_only_bridge_table(self):
        self.assertEqual(bridge_tables(SCHEMA, ["Fact", "Dimension"]), ["Bridge"])

    def test_platt_calibration_is_data_driven(self):
        model = PlattCalibrator.fit([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0])
        self.assertGreater(model.predict(0.9), model.predict(0.1))
        self.assertGreaterEqual(model.threshold, 0.0)

    def test_rejected_calibrator_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejected.json"
            path.write_text('{"status":"rejected"}', encoding="utf-8")
            self.assertIsNone(PlattCalibrator.load(path))

    def test_schema_snapshot_can_be_rebuilt_from_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            path.write_text(
                """[
                  {"source_type":"table_description","table_names":["Fact"],"aliases":["事实"]},
                  {"source_type":"column_schema","table_names":["Fact"],"field_names":["ID"]}
                ]""",
                encoding="utf-8",
            )
            snapshot = load_schema_snapshot(path)
            self.assertEqual(snapshot["Fact"]["description"], "事实")
            self.assertIn("ID", snapshot["Fact"]["columns"])

    async def test_without_calibration_only_semantic_required_tables_are_used(self):
        original = settings.table_retrieval_require_calibration
        object.__setattr__(settings, "table_retrieval_require_calibration", True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                retriever = TableRetriever(
                    FakeEmbedder(), Path(directory) / "missing.json", token_budget=1000
                )
                ir = parse_question_semantics("无已知领域的问题")
                self.assertEqual(await retriever.retrieve("未知问题", ir, SCHEMA), [])
        finally:
            object.__setattr__(
                settings, "table_retrieval_require_calibration", original
            )

    async def test_calibrated_retrieval_uses_probabilities_and_graph_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            PlattCalibrator(10.0, -5.0, 0.8, 0.99).save(path)
            retriever = TableRetriever(FakeEmbedder(), path, token_budget=1000)
            candidates = await retriever.retrieve(
                "事实与维度", parse_question_semantics("未知问题"), SCHEMA
            )
            by_name = {item.table_name: item for item in candidates}
            self.assertIn("Fact", by_name)
            self.assertIn("Dimension", by_name)
            self.assertTrue(by_name["Bridge"].is_bridge)
            self.assertIsNotNone(by_name["Fact"].probability)


if __name__ == "__main__":
    unittest.main()
