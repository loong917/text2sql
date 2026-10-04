"""Retrieval policy, cache and Prompt regressions without external services."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.evaluation_fixture import reviewed_evaluation_case
from tests.schema_fixture import synthetic_schema
from text2sql.application.context_rendering import filter_memories, format_schema_block
from text2sql.application.context_state import ContextRuntimeState
from text2sql.core.config import load_settings
from text2sql.domain.semantic_ir import MetricIntent, QueryPlan, SemanticCatalog
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.retrieval.calibrator import PlattCalibrator
from text2sql.retrieval.dataset import RetrievalExample
from text2sql.retrieval.table_card import build_table_cards, business_cards_fingerprint
from text2sql.retrieval.table_retriever import TableRetriever, select_candidates
from text2sql.retrieval.train import (
    evaluate_retrieval_policy,
    mine_training_pairs,
    train_table_retriever,
)

SCHEMA = synthetic_schema(
    {
        "Fact": {
            "description": "事实",
            "columns": {"ID": {"data_type": "int", "is_nullable": False}},
            "foreign_keys": [
                {
                    "column_name": "ID",
                    "referenced_table": "Bridge",
                    "referenced_column": "ID",
                    "constraint_name": "FK_fact_bridge",
                    "ordinal": 1,
                    "is_disabled": False,
                    "is_not_trusted": False,
                }
            ],
        },
        "Bridge": {
            "description": "桥接",
            "columns": {"ID": {"data_type": "int", "is_nullable": False}},
            "unique_keys": {"UQ_bridge": ["ID"]},
            "foreign_keys": [
                {
                    "column_name": "ID",
                    "referenced_table": "Dimension",
                    "referenced_column": "ID",
                    "constraint_name": "FK_bridge_dim",
                    "ordinal": 1,
                    "is_disabled": False,
                    "is_not_trusted": False,
                }
            ],
        },
        "Dimension": {
            "description": "维度",
            "columns": {"ID": {"data_type": "int", "is_nullable": False}},
            "unique_keys": {"UQ_dimension": ["ID"]},
            "foreign_keys": [],
        },
    }
)


def ir(*tables):
    return QueryPlan("统计", "统计", required_tables=tuple(tables))


class CountingEmbedder:
    def __init__(self):
        self.index_builds = 0
        self.fail_next = False

    async def embed(self, texts):
        if texts and texts[0].startswith("表名:"):
            self.index_builds += 1
            await asyncio.sleep(0.01)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("embedding failed")
        else:
            await asyncio.sleep(0)
        return [[1.0, 0.0] for _ in texts]


class RetrievalPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_schema_workers_close_on_failure_and_auto_fallback(self):
        for source in ("live", "auto"):
            with self.subTest(source=source):
                executor = Mock()
                executor.aclose = AsyncMock()
                config = replace(load_settings(), table_retrieval_train_schema_source=source)
                with (
                    patch("text2sql.retrieval.train.VannaSqlExecutor", return_value=executor),
                    patch(
                        "text2sql.retrieval.train.get_live_schema",
                        new=AsyncMock(side_effect=RuntimeError("database unavailable")),
                    ),
                    patch("text2sql.retrieval.train._load_frozen_schema", return_value={}),
                ):
                    with self.assertRaises(RuntimeError):
                        await train_table_retriever(config, SimpleNamespace(sql_runner=Mock()))
                executor.aclose.assert_awaited_once()

    async def test_snapshot_training_fits_hard_negatives_and_closes_embedder(self):
        class OfflineRuntime:
            @property
            def sql_runner(self):
                raise AssertionError("offline training must not contact the database")

        class TrainingEmbedder(CountingEmbedder):
            closed = False

            async def embed(self, texts):
                return [[0.0, 1.0] if "表名: Dimension" in text else [1.0, 0.0] for text in texts]

            async def aclose(self):
                self.closed = True

        schema = {"Fact": {**SCHEMA["Fact"], "foreign_keys": []}, "Dimension": SCHEMA["Dimension"]}
        catalog = SemanticCatalog(
            metrics=(
                {
                    "name": "总数",
                    "aliases": ["总数"],
                    "source_table": "Fact",
                    "aggregation": "COUNT",
                    "column": "*",
                },
            )
        )
        bundle = SimpleNamespace(
            table_cards=[],
            gold_sql=[],
            negative_sql=[],
            refusals=[],
            semantic_catalog=lambda: catalog,
        )
        embedder = TrainingEmbedder()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            snapshot.write_text(
                json.dumps(
                    {"schema_version": f"sha256:{schema_fingerprint(schema)}", "tables": schema}
                ),
                encoding="utf-8",
            )
            paths = []
            (root / "knowledge").mkdir()
            (root / "dev.jsonl").write_bytes(b"")
            (root / "test.jsonl").write_bytes(b"")
            for index, (split, sql) in enumerate(
                (
                    ("retrieval_train", "SELECT COUNT(*) FROM Fact"),
                    ("retrieval_calibration", "SELECT COUNT(DISTINCT ID) FROM Fact"),
                    ("retrieval_test", "SELECT SUM(ID) FROM Fact"),
                )
            ):
                path = root / f"{split}.jsonl"
                path.write_text(
                    json.dumps(
                        reviewed_evaluation_case(
                            {
                                "id": f"case-{index}",
                                "template_id": f"template-{index}",
                                "question": f"总数问题{index}",
                                "status": "approved",
                                "should_refuse": False,
                                "split": split,
                                "baseline_sql": sql,
                            },
                            schema=schema,
                        )
                    ),
                    encoding="utf-8",
                )
                paths.append(str(path))
            config = replace(
                load_settings(),
                table_retrieval_train_schema_source="snapshot",
                schema_snapshot_path=str(snapshot),
                retrieval_train_set_path=paths[0],
                retrieval_calibration_set_path=paths[1],
                retrieval_test_set_path=paths[2],
                table_retrieval_calibrator_path=str(root / "model.json"),
                table_retrieval_token_budget=1000,
                structured_knowledge_dir=str(root / "knowledge"),
                eval_dev_set_path=str(root / "dev.jsonl"),
                eval_test_set_path=str(root / "test.jsonl"),
            )
            with (
                patch("text2sql.retrieval.train.OllamaEmbedder", return_value=embedder),
                patch(
                    "text2sql.retrieval.train.load_validated_knowledge_bundle", return_value=bundle
                ),
            ):
                report = await train_table_retriever(config, OfflineRuntime())
            serialized_pairs = await asyncio.to_thread(
                Path(report["dataset_path"]).read_text, encoding="utf-8"
            )
            fitted = [json.loads(line) for line in serialized_pairs.splitlines()]
            self.assertEqual(report["fit_hard_negative_pairs"], 1)
            self.assertTrue(all(item["fit_sample"] for item in fitted))
            self.assertEqual(report["held_out_table_recall"], 1.0)
            self.assertTrue(report["calibrator_accepted"])
            self.assertTrue(embedder.closed)
            model = PlattCalibrator.load(root / "model.json")
            self.assertEqual(model.business_card_fingerprint, business_cards_fingerprint([]))

    def test_business_cards_enrich_only_live_schema(self):
        business = [
            {
                "table": "Fact",
                "description": "企业交易事实",
                "business_aliases": ["成交"],
                "metrics": ["销售额"],
                "dimensions": ["客户"],
                "important_columns": ["ID", "Invented"],
            },
            {"table": "Ghost", "description": "不在Schema中"},
        ]
        cards = build_table_cards(SCHEMA, business)
        self.assertEqual({card.table_name for card in cards}, set(SCHEMA))
        text = next(card.text for card in cards if card.table_name == "Fact")
        self.assertIn("销售额", text)
        self.assertIn("成交", text)
        self.assertNotIn("Invented", text)
        self.assertNotEqual(business_cards_fingerprint(business), business_cards_fingerprint([]))

    async def test_index_is_single_flight_and_business_changes_invalidate_cache(self):
        embedder = CountingEmbedder()
        retriever = TableRetriever(
            embedder,
            "missing.json",
            1000,
            "test",
            business_cards=[{"table": "Fact", "description": "旧口径"}],
        )
        await asyncio.gather(*(retriever.score_all("统计", ir("Fact"), SCHEMA) for _ in range(8)))
        self.assertEqual(embedder.index_builds, 1)
        retriever.business_cards = ({"table": "Fact", "description": "新口径"},)
        await retriever.score_all("统计", ir("Fact"), SCHEMA)
        self.assertEqual(embedder.index_builds, 2)

    async def test_failed_rebuild_keeps_previous_complete_snapshot(self):
        embedder = CountingEmbedder()
        retriever = TableRetriever(embedder, "missing.json", 1000, "test")
        await retriever.score_all("统计", ir("Fact"), SCHEMA)
        previous = retriever._index
        embedder.fail_next = True
        with self.assertRaisesRegex(RuntimeError, "embedding failed"):
            await retriever.score_all(
                "统计", ir("Fact"), {"Fact": {**SCHEMA["Fact"], "foreign_keys": []}}
            )
        self.assertIs(retriever._index, previous)
        scores = await retriever.score_all("统计", ir("Fact"), SCHEMA)
        self.assertEqual(len(scores), len(SCHEMA))

    async def test_concurrent_schema_versions_do_not_mix_cards_and_embeddings(self):
        retriever = TableRetriever(CountingEmbedder(), "missing.json", 1000, "test")
        complete, partial = await asyncio.gather(
            retriever.score_all("统计", ir("Fact"), SCHEMA),
            retriever.score_all(
                "统计", ir("Fact"), {"Fact": {**SCHEMA["Fact"], "foreign_keys": []}}
            ),
        )
        self.assertEqual({card.table_name for card, _ in complete}, set(SCHEMA))
        self.assertEqual([card.table_name for card, _ in partial], ["Fact"])

    def test_required_tables_and_bridges_survive_budget_and_report_overflow(self):
        selection = select_candidates(
            ir("Fact", "Dimension"),
            SCHEMA,
            build_table_cards(SCHEMA),
            [],
            None,
            token_budget=1,
            require_calibration=True,
        )
        self.assertEqual({item.table_name for item in selection.candidates}, set(SCHEMA))
        self.assertTrue(selection.diagnostics["budget_exceeded"])
        self.assertTrue(
            next(item.is_bridge for item in selection.candidates if item.table_name == "Bridge")
        )

    def test_optional_tables_include_bridge_cost_in_budget(self):
        cards = build_table_cards(SCHEMA)
        by_name = {card.table_name: card for card in cards}
        budget = by_name["Fact"].token_cost + by_name["Dimension"].token_cost
        selected = select_candidates(
            ir("Fact"),
            SCHEMA,
            cards,
            [(by_name["Dimension"], 1.0)],
            PlattCalibrator(10, 0, 0.5, 1.0),
            token_budget=budget,
            require_calibration=True,
        )
        self.assertEqual([item.table_name for item in selected.candidates], ["Fact"])
        self.assertFalse(selected.diagnostics["budget_exceeded"])

    def test_question_recall_uses_online_semantics_not_raw_pair_threshold(self):
        examples = [
            RetrievalExample("未识别正样本", ("Fact",), "test"),
            RetrievalExample("缺少维度", ("Fact", "Dimension"), "test"),
        ]
        cards = build_table_cards(SCHEMA)
        scores = [(card, 1.0) for card in cards]
        result = evaluate_retrieval_policy(
            examples,
            {"未识别正样本": ir(), "缺少维度": ir("Fact")},
            SCHEMA,
            {example.question: scores for example in examples},
            PlattCalibrator(1, 0, 0.999, 1.0),
            token_budget=1000,
            require_calibration=True,
            business_cards=[],
        )
        self.assertEqual(result["held_out_table_recall"], 0.0)
        self.assertEqual(result["table_pair_recall"], 1 / 3)
        self.assertEqual(result["semantic_abstained_questions"], 1)
        self.assertEqual(result["question_results"][0]["selected_tables"], [])

    def test_hard_negative_mining_changes_actual_fit_population(self):
        records = [
            {
                "question": "q",
                "table_name": "positive",
                "label": 1,
                "raw_score": 0.8,
                "hard_negative": False,
            }
        ] + [
            {
                "question": "q",
                "table_name": f"negative-{number}",
                "label": 0,
                "raw_score": number / 10,
                "hard_negative": False,
            }
            for number in range(9)
        ]
        fitted = mine_training_pairs(records)
        self.assertEqual(len(fitted), 4)
        self.assertEqual(
            {item["table_name"] for item in fitted if not item["label"]},
            {"negative-6", "negative-7", "negative-8"},
        )
        self.assertTrue(all(item["fit_sample"] for item in fitted))
        self.assertFalse(records[1]["fit_sample"])

    def test_fit_weights_affect_loss_and_business_provenance_rejects_stale_cards(self):
        balanced = PlattCalibrator.fit([0.8, 0.8], [1, 0])
        negative_heavy = PlattCalibrator.fit([0.8, 0.8], [1, 0], sample_weights=[1, 4])
        self.assertLess(negative_heavy.predict(0.8), balanced.predict(0.8))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            balanced.with_provenance(
                schema_fingerprint="schema",
                embedding_model="embed",
                dataset_fingerprint="data",
                business_card_fingerprint="old",
            ).save(path)
            self.assertIsNone(PlattCalibrator.load(path, expected_business_card_fingerprint="new"))

    def test_prompt_preserves_every_required_column_over_normal_cap(self):
        columns = {
            f"Metric{number}": {"data_type": "int", "is_nullable": False} for number in range(16)
        }
        semantics = QueryPlan(
            "统计",
            "统计",
            metrics=tuple(
                MetricIntent(f"指标{number}", "SUM", f"Metric{number}", source_table="Fact")
                for number in range(14)
            ),
        )
        rendered = format_schema_block(
            "统计",
            {"Fact": {"columns": columns}},
            ["Fact"],
            "",
            semantics,
            state=ContextRuntimeState(),
        )
        for number in range(14):
            self.assertIn(f"字段: Metric{number} ", rendered)
        self.assertNotIn("字段: Metric14 ", rendered)
        self.assertIn("语义必需字段超过常规上限", rendered)

    def test_no_unrelated_memory_fallback(self):
        self.assertEqual(
            filter_memories(["无关业务规则"], ["Fact"], state=ContextRuntimeState()), []
        )
        self.assertEqual(filter_memories(["Fact 规则"], [], state=ContextRuntimeState()), [])
