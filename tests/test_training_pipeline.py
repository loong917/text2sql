"""Offline artifact building contracts with isolated stores and fake adapters."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.evaluation_fixture import evaluation_split_cases, reviewed_evaluation_case
from tests.schema_fixture import synthetic_schema
from tests.test_production_readiness import synthetic_collection_evidence
from text2sql.application.context_state import ContextRuntimeState
from text2sql.core.config import load_settings
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry
from text2sql.knowledge.models import TableCardRecord
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.snapshot import ArtifactSnapshot
from text2sql.knowledge.structured import KnowledgeBundle
from text2sql.retrieval.calibrator import PlattCalibrator
from text2sql.retrieval.dataset import retrieval_dataset_fingerprint
from text2sql.retrieval.table_card import business_cards_fingerprint
from text2sql.training.fingerprint import gold_feedback_fingerprint, should_skip_training
from text2sql.training.knowledge_builder import train_structured_knowledge
from text2sql.training.pipeline import build_candidate, train_knowledge_async
from text2sql.training.records import (
    build_index_record,
    dedupe_index_records,
    extract_sql_table_names,
)
from text2sql.training.storage import write_json_file

SCHEMA = synthetic_schema(
    {
        "Fact": {
            "description": "事实表",
            "columns": {"ID": {"data_type": "int", "is_nullable": False, "description": "编号"}},
            "foreign_keys": [],
        }
    }
)


class Memory:
    def __init__(self):
        self.saved = []

    async def save_text_memory(self, content, context):
        self.saved.append(content)


class Runtime:
    def __init__(self):
        self.memory = Memory()
        self.collections = {}

    def create_knowledge_memory(self, *, collection_name):
        self.collection_name = collection_name
        self.memory = Memory()
        self.collections[collection_name] = self.memory
        return self.memory

    def collection_evidence(self, collection_name, *, index_records=None):
        return synthetic_collection_evidence(
            self.collections[collection_name].saved, index_records=index_records
        )


class TrainingPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_negative_isolation_runs_before_any_candidate_or_collection_write(self):
        for status, sql, error_types, rejected in (
            ("approved", "SELECT ID AS Renamed FROM dbo.Fact", ["semantic_error"], True),
            ("approved", "SELECT ID AS Renamed FROM dbo.Fact", ["syntax_error"], True),
            ("approved", "SELECT ID FROM Fact WHERE (", ["syntax_error"], True),
            ("approved", "SELECT ID FROM Fact ORDER BY", ["syntax_error"], True),
            ("approved", "SELECT ID FROM Fact GROUP BY", ["syntax_error"], True),
            ("approved", "SELECT ID FROM Fact UNION", ["syntax_error"], True),
            ("approved", "SELECT ID FROM Fact UNION SELECT (", ["syntax_error"], True),
            ("approved", "SELECT (", ["syntax_error"], False),
            ("candidate", "SELECT ID FROM Fact", ["semantic_error"], False),
            ("rejected", "SELECT ID FROM Fact", ["semantic_error"], False),
        ):
            with self.subTest(status=status, sql=sql, error_types=error_types):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "knowledge").mkdir()
                    splits = (
                        "retrieval_train",
                        "retrieval_calibration",
                        "retrieval_test",
                        "dev",
                        "test",
                    )
                    for split in splits:
                        (root / f"{split}.jsonl").write_bytes(b"")
                    held_out = evaluation_split_cases("test")[0]
                    held_out.update(baseline_sql="SELECT ID FROM Fact")
                    held_out = reviewed_evaluation_case(held_out, schema=SCHEMA)
                    (root / "test.jsonl").write_text(json.dumps(held_out), encoding="utf-8")
                    (root / "calibrator.json").write_bytes(b"unread past isolation")
                    config = replace(
                        load_settings(),
                        app_env="development",
                        structured_knowledge_dir=str(root / "knowledge"),
                        table_retrieval_calibrator_path=str(root / "calibrator.json"),
                        retrieval_train_set_path=str(root / "retrieval_train.jsonl"),
                        retrieval_calibration_set_path=str(root / "retrieval_calibration.jsonl"),
                        retrieval_test_set_path=str(root / "retrieval_test.jsonl"),
                        eval_dev_set_path=str(root / "dev.jsonl"),
                        eval_test_set_path=str(root / "test.jsonl"),
                    )
                    bundle = KnowledgeBundle(
                        files=["synthetic-fixture"],
                        negative_sql=[
                            {
                                "id": "distinct-negative",
                                "query_family_id": "distinct-negative-family",
                                "question": "separately worded negative",
                                "sql": sql,
                                "status": status,
                                "error_types": error_types,
                            }
                        ],
                    )
                    runtime = SimpleNamespace(create_knowledge_memory=Mock())
                    repository = SimpleNamespace(load_gold=Mock(return_value=[]), count=Mock())
                    with (
                        patch(
                            "text2sql.training.pipeline.get_live_schema",
                            new=AsyncMock(return_value=SCHEMA),
                        ),
                        patch(
                            "text2sql.training.pipeline.SQLiteFeedbackRepository",
                            return_value=repository,
                        ),
                        patch(
                            "text2sql.training.pipeline.load_validated_knowledge_bundle",
                            return_value=bundle,
                        ),
                        patch(
                            "text2sql.training.pipeline.should_skip_training", return_value=True
                        ) as skip,
                        patch("text2sql.training.pipeline.KnowledgeArtifactRegistry") as registry,
                    ):
                        if rejected:
                            with self.assertRaisesRegex(
                                ValueError, "SQL template leakage|cannot safely isolate"
                            ):
                                await build_candidate(
                                    config, runtime, object(), False, 0, ContextRuntimeState()
                                )
                            skip.assert_not_called()
                        else:
                            await build_candidate(
                                config, runtime, object(), False, 0, ContextRuntimeState()
                            )
                            skip.assert_called_once()
                        registry.assert_not_called()
                    runtime.create_knowledge_memory.assert_not_called()

    def test_gold_fingerprint_ignores_storage_metadata_and_record_order(self):
        first = {
            "question": "事实编号",
            "sql": "SELECT ID FROM Fact",
            "candidate_tables": ["Fact", "Dimension"],
            "promotion_evidence": {"schema_fingerprint": "schema", "reviewed": True},
            "updated_at": "old",
            "tier": "gold",
        }
        second = {**first, "question": "所有事实编号"}
        equivalent = {
            **first,
            "candidate_tables": ["Dimension", "Fact"],
            "updated_at": "new",
            "captured_at": "later",
            "result_row_count": 100,
        }
        original = gold_feedback_fingerprint([first, second])
        self.assertEqual(original, gold_feedback_fingerprint([second, equivalent]))
        self.assertNotEqual(
            original, gold_feedback_fingerprint([{**first, "sql": "SELECT 1"}, second])
        )
        self.assertNotEqual(
            original,
            gold_feedback_fingerprint(
                [{**first, "promotion_evidence": {"schema_fingerprint": "changed"}}, second]
            ),
        )

    def test_record_dedupe_has_no_unused_priority_and_cte_is_not_a_table(self):
        first = build_index_record("定义", "metric_rule", table_names=["Fact"], aliases=["甲"])
        second = build_index_record("定义", "metric_rule", table_names=["Fact"], aliases=["乙"])
        records, removed = dedupe_index_records([first, second])
        self.assertEqual(removed, 1)
        self.assertEqual(records[0]["aliases"], ["甲", "乙"])
        self.assertNotIn("priority", records[0])
        self.assertEqual(
            extract_sql_table_names("WITH c AS (SELECT ID FROM Fact) SELECT ID FROM c"), ["Fact"]
        )

    async def test_only_approved_negative_and_refusal_records_are_compiled(self):
        negative = {
            "id": "negative",
            "question": "错误问题",
            "sql": "SELECT Bad FROM Fact",
            "error_types": ["unknown_column"],
            "status": "approved",
        }
        bundle = KnowledgeBundle(
            files=["fixture"],
            negative_sql=[negative, {**negative, "id": "candidate", "status": "candidate"}],
            refusals=[
                {"id": "refusal", "question": "无法回答", "reason": "无指标", "status": "approved"},
                {"id": "pending", "question": "待审核", "reason": "无指标", "status": "candidate"},
            ],
        )
        memory = Memory()
        records = []
        report = {"warnings": []}
        await train_structured_knowledge(memory, object(), bundle, records, report, None)
        self.assertEqual(
            [record["source_type"] for record in records],
            ["negative_sql_example", "refusal_example"],
        )
        self.assertEqual(report["negative_sql_examples_trained"], 1)
        self.assertEqual(len(memory.saved), 2)

    async def test_worker_is_closed_on_build_failure_and_state_is_reset(self):
        executor = SimpleNamespace(aclose=AsyncMock())
        state = ContextRuntimeState(live_schema=SCHEMA)
        with (
            patch("text2sql.training.pipeline.VannaSqlExecutor", return_value=executor),
            patch(
                "text2sql.training.pipeline.build_candidate",
                new=AsyncMock(side_effect=ValueError("invalid knowledge")),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "invalid knowledge"):
                await train_knowledge_async(
                    load_settings(), SimpleNamespace(sql_runner=object()), False, 0, state
                )
        executor.aclose.assert_awaited_once()
        self.assertIsNone(state.live_schema)

    async def test_snapshot_precedes_evaluation_and_rejection_keeps_active_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = KnowledgeBundle(
                files=["fixture"],
                table_cards=[
                    TableCardRecord(
                        **{
                            "table": "Fact",
                            "description": "企业事实",
                            "business_aliases": ["记录"],
                            "metrics": [],
                            "dimensions": [],
                            "important_columns": ["ID"],
                        }
                    ).model_dump()
                ],
            )
            config = replace(
                load_settings(),
                app_env="development",
                knowledge_artifact_dir=str(root / "artifacts"),
                knowledge_active_pointer_path=str(root / "active.json"),
                table_retrieval_calibrator_path=str(root / "source_calibrator.json"),
                feedback_db_path=str(root / "feedback.sqlite3"),
                training_state_path=str(root / "state.json"),
                training_skip_unchanged=False,
                structured_knowledge_dir=str(root / "knowledge"),
                retrieval_train_set_path=str(root / "retrieval_train.jsonl"),
                retrieval_calibration_set_path=str(root / "retrieval_calibration.jsonl"),
                retrieval_test_set_path=str(root / "retrieval_test.jsonl"),
                eval_dev_set_path=str(root / "dev.jsonl"),
                eval_test_set_path=str(root / "test.jsonl"),
            )
            (root / "knowledge").mkdir()
            for split in (
                "retrieval_train",
                "retrieval_calibration",
                "retrieval_test",
                "dev",
                "test",
            ):
                (root / f"{split}.jsonl").write_bytes(b"")
            digest = retrieval_dataset_fingerprint(
                config.retrieval_train_set_path,
                config.retrieval_calibration_set_path,
                config.retrieval_test_set_path,
            )
            PlattCalibrator(1, 0, 0.5, 1.0).with_provenance(
                schema_fingerprint=schema_fingerprint(SCHEMA),
                embedding_model=config.embedding_model,
                dataset_fingerprint=digest,
                business_card_fingerprint=business_cards_fingerprint(bundle.table_cards),
            ).save(Path(config.table_retrieval_calibrator_path))
            observed_snapshots = []

            async def evaluate(*args, knowledge_index_path, **kwargs):
                snapshot = await asyncio.to_thread(
                    ArtifactSnapshot.load,
                    Path(knowledge_index_path).parent / "knowledge_snapshot.json",
                )
                self.assertEqual(snapshot.knowledge.fingerprint, bundle.fingerprint)
                self.assertEqual(snapshot.retrieval_dataset_fingerprint, digest)
                observed_snapshots.append(snapshot)
                return []

            runtime = Runtime()
            gold = [{"question": "事实编号", "sql": "SELECT ID FROM Fact"}]
            feedback_repository = SimpleNamespace(
                load_gold=Mock(return_value=gold), count=Mock(return_value=1)
            )
            source_path = Path(config.table_retrieval_calibrator_path)
            pinned_calibrator = await asyncio.to_thread(source_path.read_bytes)
            validate_fixed_bytes = PlattCalibrator.from_bytes
            create_memory = runtime.create_knowledge_memory

            def validate_then_replace(content, **kwargs):
                verified = validate_fixed_bytes(content, **kwargs)
                self.assertIsNotNone(verified)
                source_path.write_bytes(b"temporarily replaced unverified bytes")
                return verified

            def consume_fixed_bytes_then_restore(*, collection_name):
                copied = next((root / "artifacts").glob("*/table_retrieval_calibrator.json"))
                self.assertEqual(copied.read_bytes(), pinned_calibrator)
                self.assertNotEqual(source_path.read_bytes(), pinned_calibrator)
                source_path.write_bytes(pinned_calibrator)
                return create_memory(collection_name=collection_name)

            with (
                patch(
                    "text2sql.training.pipeline.get_live_schema", new=AsyncMock(return_value=SCHEMA)
                ),
                patch(
                    "text2sql.training.pipeline.SQLiteFeedbackRepository",
                    return_value=feedback_repository,
                ),
                patch(
                    "text2sql.training.pipeline.load_validated_knowledge_bundle",
                    return_value=bundle,
                ),
                patch("text2sql.training.pipeline.ToolContext", return_value=object()),
                patch("text2sql.evaluation.wiring.run_configured_evaluation", new=evaluate),
                patch(
                    "text2sql.training.pipeline.evaluate_quality_gate",
                    side_effect=[
                        {"passed": True, "failures": []},
                        {"passed": True, "failures": []},
                        {"passed": False, "failures": ["evaluation failed"]},
                    ],
                ),
            ):
                with (
                    patch(
                        "text2sql.training.pipeline.PlattCalibrator.from_bytes",
                        side_effect=validate_then_replace,
                    ),
                    patch.object(
                        runtime,
                        "create_knowledge_memory",
                        side_effect=consume_fixed_bytes_then_restore,
                    ),
                ):
                    await build_candidate(
                        config, runtime, object(), False, 0, ContextRuntimeState()
                    )
                registry = KnowledgeArtifactRegistry(
                    config.knowledge_artifact_dir, config.knowledge_active_pointer_path
                )
                accepted = registry.load_candidate()
                self.assertIsNotNone(accepted)
                self.assertIsNone(registry.load_active())
                dev_report = json.loads(
                    await asyncio.to_thread(Path(accepted.report_path).read_bytes)
                )
                self.assertEqual(dev_report["artifact_status"], "candidate")
                manifest = await asyncio.to_thread(
                    Path(accepted.manifest_path).read_text, encoding="utf-8"
                )
                self.assertIn("knowledge_snapshot_sha256", json.loads(manifest)["outputs"])
                self.assertEqual(
                    json.loads(manifest)["inputs"]["feedback_gold_hash"],
                    gold_feedback_fingerprint(gold),
                )
                self.assertIn(
                    "运行期反馈正确样本:\n问题: 事实编号\nSQL:\nSELECT ID FROM Fact",
                    runtime.memory.saved,
                )
                registry.publish(accepted)  # Model an independently approved existing version.
                active_pointer = await asyncio.to_thread(registry.active_pointer.read_bytes)
                await build_candidate(config, runtime, object(), False, 0, ContextRuntimeState())
                staged = registry.load_candidate()
                self.assertIsNotNone(staged)
                self.assertNotEqual(staged.version, accepted.version)
                self.assertEqual(registry.load_active(), accepted)
                self.assertEqual(
                    await asyncio.to_thread(registry.active_pointer.read_bytes), active_pointer
                )
                with self.assertRaisesRegex(RuntimeError, "evaluation failed"):
                    await build_candidate(
                        config, runtime, object(), False, 0, ContextRuntimeState()
                    )
                self.assertEqual(registry.load_active().version, accepted.version)
                self.assertEqual(registry.load_candidate(), staged)
                self.assertEqual(
                    await asyncio.to_thread(registry.active_pointer.read_bytes), active_pointer
                )
            self.assertEqual(len(observed_snapshots), 3)
            self.assertEqual(feedback_repository.load_gold.call_count, 3)
            feedback_repository.load_gold.assert_called_with(
                current_schema_fingerprint=schema_fingerprint(SCHEMA)
            )

    def test_skip_requires_a_valid_active_artifact_not_just_pointer_existence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(
                load_settings(),
                knowledge_artifact_dir=str(root / "artifacts"),
                knowledge_active_pointer_path=str(root / "active.json"),
                training_state_path=str(root / "state.json"),
                training_skip_unchanged=True,
            )
            write_json_file(config.training_state_path, {"fingerprint": {"input": "same"}})
            write_json_file(config.knowledge_active_pointer_path, {"version": "incomplete"})
            self.assertFalse(should_skip_training(config, {"input": "same"}))
