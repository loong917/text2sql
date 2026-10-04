import json
import tempfile
import unittest
from pathlib import Path

from tests.schema_fixture import synthetic_schema
from text2sql.domain.semantic_ir import QueryPlan, parse_question_semantics
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.retrieval.calibrator import PlattCalibrator
from text2sql.retrieval.dataset import RetrievalExample, extract_sql_tables, serialize_pair_records
from text2sql.retrieval.schema_graph import bridge_tables
from text2sql.retrieval.table_card import (
    build_table_cards,
    load_schema_snapshot,
)
from text2sql.retrieval.table_retriever import TableRetriever
from text2sql.retrieval.train import publish_calibrator_candidate

SCHEMA = synthetic_schema(
    {
        "Fact": {
            "description": "事实表",
            "columns": {"DimID": {"data_type": "int", "description": "维度编号"}},
            "foreign_keys": [
                {
                    "column_name": "DimID",
                    "referenced_table": "Bridge",
                    "referenced_column": "DimID",
                    "constraint_name": "FK_fact_bridge",
                    "ordinal": 1,
                    "is_disabled": False,
                    "is_not_trusted": False,
                }
            ],
        },
        "Bridge": {
            "description": "桥接表",
            "columns": {"DimID": {"data_type": "int", "description": ""}},
            "unique_keys": {"UQ_bridge": ["DimID"]},
            "foreign_keys": [
                {
                    "column_name": "DimID",
                    "referenced_table": "Dimension",
                    "referenced_column": "DimID",
                    "constraint_name": "FK_bridge_dim",
                    "ordinal": 1,
                    "is_disabled": False,
                    "is_not_trusted": False,
                }
            ],
        },
        "Dimension": {
            "description": "维度表",
            "columns": {
                "DimID": {"data_type": "int", "description": "维度编号"},
                "Name": {"data_type": "nvarchar", "description": "名称"},
            },
            "unique_keys": {"UQ_dimension": ["DimID"]},
            "foreign_keys": [],
        },
    }
)


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
        records = serialize_pair_records([RetrievalExample("问题", tables, "test")], SCHEMA)
        labels = {item["table_name"]: item["label"] for item in records}
        self.assertEqual(labels, {"Fact": 1, "Bridge": 0, "Dimension": 1})

    def test_schema_graph_adds_only_bridge_table(self):
        self.assertEqual(bridge_tables(SCHEMA, ["Fact", "Dimension"]), ["Bridge"])

    def test_platt_calibration_is_data_driven(self):
        model = PlattCalibrator.fit([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0])
        self.assertGreater(model.predict(0.9), model.predict(0.1))
        self.assertGreaterEqual(model.threshold, 0.0)

    def test_threshold_is_selected_on_held_out_calibration_scores(self):
        model = PlattCalibrator.fit([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1])
        calibrated = model.calibrate_threshold([0.3, 0.7, 0.75], [0, 1, 1], target_recall=1.0)
        self.assertEqual(
            calibrated.threshold,
            min(calibrated.predict(0.7), calibrated.predict(0.75)),
        )

    def test_rejected_calibrator_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejected.json"
            path.write_text('{"status":"rejected"}', encoding="utf-8")
            self.assertIsNone(PlattCalibrator.load(path))

    def test_versioned_schema_snapshot_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": f"sha256:{schema_fingerprint(SCHEMA)}",
                        "tables": SCHEMA,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_schema_snapshot(path), SCHEMA)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["tables"]["Fact"]["description"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "指纹不匹配"):
                load_schema_snapshot(path)

    async def test_without_calibration_only_semantic_required_tables_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = TableRetriever(
                FakeEmbedder(),
                Path(directory) / "missing.json",
                token_budget=1000,
                embedding_model="test-embedding",
            )
            ir = parse_question_semantics("无已知领域的问题")
            self.assertEqual(await retriever.retrieve("未知问题", ir, SCHEMA), [])

    async def test_calibrated_retrieval_uses_probabilities_and_graph_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            PlattCalibrator(10.0, -5.0, 0.8, 0.99).with_provenance(
                schema_fingerprint=schema_fingerprint(SCHEMA),
                embedding_model="test-embedding",
                dataset_fingerprint="test-dataset",
            ).save(path)
            retriever = TableRetriever(
                FakeEmbedder(),
                path,
                token_budget=1000,
                embedding_model="test-embedding",
            )
            candidates = await retriever.retrieve(
                "事实与维度",
                QueryPlan(
                    original_question="事实与维度",
                    normalized_question="事实与维度",
                    required_tables=("Fact", "Dimension"),
                ),
                SCHEMA,
            )
            by_name = {item.table_name: item for item in candidates}
            self.assertIn("Fact", by_name)
            self.assertIn("Dimension", by_name)
            self.assertTrue(by_name["Bridge"].is_bridge)
            self.assertIsNotNone(by_name["Fact"].probability)

    async def test_calibrated_retriever_abstains_without_catalog_grounding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            PlattCalibrator(10.0, -5.0, 0.1, 0.99).with_provenance(
                schema_fingerprint=schema_fingerprint(SCHEMA),
                embedding_model="test-embedding",
                dataset_fingerprint="test-dataset",
            ).save(path)
            retriever = TableRetriever(
                FakeEmbedder(),
                path,
                token_budget=1000,
                embedding_model="test-embedding",
            )

            candidates = await retriever.retrieve(
                "统计血液运输车辆油耗",
                parse_question_semantics("统计血液运输车辆油耗"),
                SCHEMA,
            )

            self.assertEqual(candidates, [])

    def test_calibrator_is_rejected_after_schema_or_embedding_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            PlattCalibrator(1.0, 0.0, 0.5, 0.99).with_provenance(
                schema_fingerprint="schema-a",
                embedding_model="embedding-a",
                dataset_fingerprint="dataset-a",
            ).save(path)
            self.assertIsNone(
                PlattCalibrator.load(
                    path,
                    expected_schema_fingerprint="schema-b",
                    expected_embedding_model="embedding-a",
                )
            )
            self.assertIsNone(
                PlattCalibrator.load(
                    path,
                    expected_schema_fingerprint="schema-a",
                    expected_embedding_model="embedding-a",
                    expected_dataset_fingerprint="dataset-b",
                )
            )
            self.assertIsNone(
                PlattCalibrator.load(
                    path,
                    expected_schema_fingerprint="schema-a",
                    expected_embedding_model="embedding-b",
                )
            )

    def test_rejected_candidate_keeps_last_known_good_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            active = PlattCalibrator(1.0, 0.0, 0.5, 0.99).with_provenance(
                schema_fingerprint="schema",
                embedding_model="embedding",
                dataset_fingerprint="active",
            )
            active.save(path)
            candidate = PlattCalibrator(2.0, 0.0, 0.7, 0.99).with_provenance(
                schema_fingerprint="schema",
                embedding_model="embedding",
                dataset_fingerprint="candidate",
            )

            result = publish_calibrator_candidate(
                candidate,
                path,
                accepted=False,
                table_recall=0.8,
                false_positive_rate=0.4,
                maximum_false_positive_rate=0.25,
            )

            self.assertTrue(result["active_artifact_retained"])
            self.assertEqual(PlattCalibrator.load(path).dataset_fingerprint, "active")
            self.assertTrue(Path(result["rejected_artifact_path"]).exists())


if __name__ == "__main__":
    unittest.main()
