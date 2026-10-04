"""Immutable training input and calibrator publication contracts, without services."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.evaluation_fixture import reviewed_evaluation_case
from tests.schema_fixture import synthetic_schema
from text2sql.core.config import load_settings
from text2sql.domain.semantic_ir import SemanticCatalog
from text2sql.knowledge.input_snapshot import InputSnapshot, InputSnapshotError
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.retrieval.calibrator import PlattCalibrator
from text2sql.retrieval.dataset import load_retrieval_examples, retrieval_dataset_fingerprint
from text2sql.retrieval.train import (
    _pin_models,
    calibrator_lease,
    publish_calibrator_candidate,
    train_table_retriever,
)


class InputSnapshotTests(unittest.TestCase):
    def test_fixed_bytes_and_fingerprint_survive_aba(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "split.jsonl"
            path.write_bytes(b"original")
            inputs = InputSnapshot()
            self.assertEqual(inputs.require_read(path), b"original")
            path.write_bytes(b"replacement")
            self.assertEqual(inputs.read(path), b"original")
            self.assertEqual(inputs.sha256(path), sha256(b"original").hexdigest())
            with self.assertRaises(InputSnapshotError):
                inputs.assert_unchanged()
            path.write_bytes(b"original")
            inputs.assert_unchanged()

    def test_membership_missing_files_and_identity_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = InputSnapshot()
            self.assertIsNone(inputs.read(root / "missing"))
            self.assertEqual(inputs.sha256(root / "missing"), "missing")
            with self.assertRaises(InputSnapshotError):
                inputs.require_read(root / "missing")
            inputs.pin_directory(root / "absent")
            (root / "absent").mkdir()
            self.assertIn(str(root / "absent"), inputs.changed_paths())
            (root / "missing").write_bytes(b"created")
            self.assertIn(str(root / "missing"), inputs.changed_paths())
            identity = ["v1"]
            inputs.pin_identity("model", identity, lambda: identity)
            identity.append("v2")
            self.assertIn("identity:model", inputs.changed_paths())
            with self.assertRaises(ValueError):
                inputs.pin_identity("model", [], lambda: [])

    def test_directory_changes_and_observer_failure_reject_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.json").write_bytes(b"first")
            inputs = InputSnapshot()
            self.assertEqual(inputs.pin_directory(root), (root / "first.json",))
            (root / "second.json").write_bytes(b"second")
            self.assertIn(str(root), inputs.changed_paths())
            inputs.pin_identity("unavailable", "v1", Mock(side_effect=RuntimeError("offline")))
            self.assertIn("identity:unavailable", inputs.changed_paths())

    def test_example_parser_and_dataset_hash_use_identical_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "train.jsonl"
            content = json.dumps(
                reviewed_evaluation_case(
                    {
                        "id": "training",
                        "template_id": "training",
                        "question": "synthetic count",
                        "status": "approved",
                        "should_refuse": False,
                        "split": "retrieval_train",
                        "baseline_sql": "SELECT COUNT(*) FROM Fact",
                    }
                )
            ).encode()
            path.write_bytes(content)
            inputs = InputSnapshot()
            examples = load_retrieval_examples(path, reader=inputs.read)
            digest = retrieval_dataset_fingerprint(path, reader=inputs.read)
            path.write_bytes(b"not approved replacement")
            self.assertEqual(load_retrieval_examples(path, reader=inputs.read), examples)
            self.assertEqual(retrieval_dataset_fingerprint(path, reader=inputs.read), digest)
            self.assertEqual(examples[0].positive_tables, ("Fact",))
            with self.assertRaises(InputSnapshotError):
                inputs.assert_unchanged()

    def test_dataset_fingerprint_frames_file_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a", Path(directory) / "b"
            first.write_bytes(b"a")
            second.write_bytes(b"bc")
            before = retrieval_dataset_fingerprint(first, second)
            first.write_bytes(b"ab")
            second.write_bytes(b"c")
            self.assertNotEqual(before, retrieval_dataset_fingerprint(first, second))


class CalibratorBytesTests(unittest.TestCase):
    def test_from_bytes_rejects_nonfinite_and_coerced_numbers(self):
        original = asdict(PlattCalibrator(1, 0, 0.5, 0.99))
        for key in ("slope", "intercept", "threshold", "target_recall"):
            for invalid in (float("nan"), float("inf"), -float("inf"), True, "0.5", None):
                with self.subTest(key=key, invalid=invalid):
                    self.assertIsNone(
                        PlattCalibrator.from_bytes(json.dumps({**original, key: invalid}).encode())
                    )
        for key, invalid in (
            ("threshold", -0.1),
            ("threshold", 1.1),
            ("target_recall", 0),
            ("target_recall", 1.1),
            ("artifact_version", True),
            ("status", "rejected"),
        ):
            with self.subTest(key=key, invalid=invalid):
                self.assertIsNone(
                    PlattCalibrator.from_bytes(json.dumps({**original, key: invalid}).encode())
                )
        for content in (b"[]", b"null", b"{}", b"\xff", b"not-json"):
            self.assertIsNone(PlattCalibrator.from_bytes(content))

    def test_verified_bytes_bind_provenance_and_actual_model_digest(self):
        candidate = PlattCalibrator(1, 0, 0.5, 0.99).with_provenance(
            schema_fingerprint="schema",
            embedding_model="embed",
            dataset_fingerprint="data",
            business_card_fingerprint="business",
            embedding_model_digest="digest-v1",
        )
        content = candidate.to_bytes()
        self.assertEqual(
            PlattCalibrator.from_bytes(content, expected_embedding_model_digest="digest-v1"),
            candidate,
        )
        self.assertIsNone(
            PlattCalibrator.from_bytes(content, expected_embedding_model_digest="digest-v2")
        )
        self.assertIsNone(
            PlattCalibrator.from_bytes(content, expected_dataset_fingerprint="changed")
        )
        legacy = PlattCalibrator(1, 0, 0.5, 0.99).to_bytes()
        self.assertIsNone(
            PlattCalibrator.from_bytes(legacy, expected_embedding_model_digest="digest-v1")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            path.write_bytes(content)
            self.assertEqual(
                PlattCalibrator.load(path, expected_embedding_model_digest="digest-v1"), candidate
            )

    def test_invalid_training_inputs_are_not_silently_clamped(self):
        with self.assertRaises(ValueError):
            PlattCalibrator.fit([float("nan"), 0.5], [0, 1])
        with self.assertRaises(ValueError):
            PlattCalibrator.fit([0, 0.5], [False, True])
        with self.assertRaises(ValueError):
            PlattCalibrator(1, 0, 0.5, 0.99).predict(float("inf"))


class CalibratorPublicationTests(unittest.TestCase):
    def test_lease_is_exclusive_across_processes_and_cleanup_is_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            with calibrator_lease(path) as lease:
                command = (
                    "import sys\nfrom pathlib import Path\n"
                    "from text2sql.retrieval.train import calibrator_lease\n"
                    "try:\n with calibrator_lease(Path(sys.argv[1])): pass\n"
                    "except RuntimeError: sys.exit(27)\n"
                )
                environment = {
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                }
                result = subprocess.run(
                    [sys.executable, "-c", command, str(path)],
                    capture_output=True,
                    timeout=30,
                    env=environment,
                    check=False,
                )
                self.assertEqual(result.returncode, 27, result.stderr.decode())
                lease.assert_owned(path)
            self.assertFalse(lease.path.exists())
            with calibrator_lease(path):
                pass

    def test_changed_lease_is_not_removed_or_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            with calibrator_lease(path) as lease:
                lease.path.write_bytes(b"[]")
                with self.assertRaises(RuntimeError):
                    lease.assert_owned(path)
            self.assertTrue(lease.path.exists())

    def test_guard_failure_preserves_old_bytes_and_cleans_unique_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            original = PlattCalibrator(1, 0, 0.5, 0.99).to_bytes()
            path.write_bytes(original)
            with self.assertRaises(InputSnapshotError):
                publish_calibrator_candidate(
                    PlattCalibrator(2, 0, 0.5, 0.99),
                    path,
                    accepted=True,
                    table_recall=1,
                    false_positive_rate=0,
                    maximum_false_positive_rate=0.25,
                    pre_publish=Mock(side_effect=InputSnapshotError("changed input")),
                )
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".*.candidate.*.tmp")), [])
            self.assertEqual(list(path.parent.glob("*.training.lock")), [])

    def test_replace_failure_and_invalid_candidate_preserve_previous_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibrator.json"
            original = PlattCalibrator(1, 0, 0.5, 0.99).to_bytes()
            path.write_bytes(original)
            with patch("text2sql.retrieval.train.os.replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    publish_calibrator_candidate(
                        PlattCalibrator(2, 0, 0.5, 0.99),
                        path,
                        accepted=True,
                        table_recall=1,
                        false_positive_rate=0,
                        maximum_false_positive_rate=0.25,
                    )
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ValueError):
                publish_calibrator_candidate(
                    PlattCalibrator(2, 0, 2, 0.99),
                    path,
                    accepted=True,
                    table_recall=1,
                    false_positive_rate=0,
                    maximum_false_positive_rate=0.25,
                )
            self.assertEqual(path.read_bytes(), original)


SCHEMA = synthetic_schema(
    {
        "Fact": {"columns": {"Value": {"data_type": "int"}}},
        "Other": {"columns": {"Value": {"data_type": "int"}}},
    }
)
CATALOG = SemanticCatalog(
    metrics=(
        {
            "name": "total",
            "aliases": ["total"],
            "source_table": "Fact",
            "aggregation": "COUNT",
            "column": "*",
        },
    )
)


class SyntheticEmbedder:
    def __init__(self, hook=lambda: None):
        self.hook = hook
        self.closed = False

    async def embed(self, texts):
        self.hook()
        return [[0.0, 1.0] if "表名: Other" in text else [1.0, 0.0] for text in texts]

    async def aclose(self):
        self.closed = True


def training_fixture(root):
    snapshot = root / "schema.json"
    snapshot.write_text(
        json.dumps({"schema_version": f"sha256:{schema_fingerprint(SCHEMA)}", "tables": SCHEMA}),
        encoding="utf-8",
    )
    paths = []
    for index, (split, sql) in enumerate(
        (
            ("retrieval_train", "SELECT COUNT(*) FROM Fact"),
            ("retrieval_calibration", "SELECT COUNT(DISTINCT Value) FROM Fact"),
            ("retrieval_test", "SELECT SUM(Value) FROM Fact"),
        )
    ):
        path = root / f"{split}.jsonl"
        path.write_text(
            json.dumps(
                reviewed_evaluation_case(
                    {
                        "id": f"case-{index}",
                        "template_id": f"family-{index}",
                        "question": f"total question {index}",
                        "status": "approved",
                        "should_refuse": False,
                        "split": split,
                        "baseline_sql": sql,
                    },
                    schema=SCHEMA,
                )
            ),
            encoding="utf-8",
        )
        paths.append(str(path))
    (root / "knowledge").mkdir()
    (root / "dev.jsonl").write_bytes(b"")
    (root / "test.jsonl").write_bytes(b"")
    config = replace(
        load_settings(),
        table_retrieval_train_schema_source="snapshot",
        schema_snapshot_path=str(snapshot),
        retrieval_train_set_path=paths[0],
        retrieval_calibration_set_path=paths[1],
        retrieval_test_set_path=paths[2],
        eval_dev_set_path=str(root / "dev.jsonl"),
        eval_test_set_path=str(root / "test.jsonl"),
        structured_knowledge_dir=str(root / "knowledge"),
        table_retrieval_calibrator_path=str(root / "model.json"),
        table_retrieval_token_budget=1000,
        llm_model="llm",
        embedding_model="embed",
        llm_model_digest="",
        embedding_model_digest="",
    )
    PlattCalibrator(1, 0, 0.5, 0.99).save(root / "model.json")
    bundle = SimpleNamespace(
        table_cards=[], gold_sql=[], negative_sql=[], refusals=[], semantic_catalog=lambda: CATALOG
    )
    return config, bundle


class RetrievalTrainingSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_negative_holdout_is_checked_before_embedding_or_model_publication(self):
        for status, sql, error_types, rejected in (
            (
                "approved",
                "SELECT SUM(f.Value) AS OtherLabel FROM Fact AS f",
                ["semantic_error"],
                True,
            ),
            ("approved", "SELECT SUM(Value) FROM Fact", ["syntax_error"], True),
            ("approved", "SELECT SUM(Value) FROM Fact WHERE (", ["syntax_error"], True),
            ("approved", "SELECT SUM(Value) FROM Fact ORDER BY", ["syntax_error"], True),
            ("approved", "SELECT SUM(Value) FROM Fact GROUP BY", ["syntax_error"], True),
            ("approved", "SELECT SUM(Value) FROM Fact UNION", ["syntax_error"], True),
            ("approved", "SELECT SUM(Value) FROM Fact UNION SELECT (", ["syntax_error"], True),
            ("approved", "SELECT (", ["syntax_error"], False),
            ("candidate", "SELECT SUM(Value) FROM Fact", ["semantic_error"], False),
            ("rejected", "SELECT SUM(Value) FROM Fact", ["semantic_error"], False),
        ):
            with self.subTest(status=status, sql=sql, error_types=error_types):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config, bundle = training_fixture(root)
                    old = (root / "model.json").read_bytes()
                    bundle.negative_sql = [
                        {
                            "id": "distinct-negative",
                            "query_family_id": "distinct-negative-family",
                            "question": "separately worded negative",
                            "sql": sql,
                            "status": status,
                            "error_types": error_types,
                        }
                    ]
                    embedder = SyntheticEmbedder()
                    with (
                        patch(
                            "text2sql.retrieval.train.OllamaEmbedder", return_value=embedder
                        ) as create_embedder,
                        patch(
                            "text2sql.retrieval.train.load_validated_knowledge_bundle",
                            return_value=bundle,
                        ),
                    ):
                        if rejected:
                            with self.assertRaisesRegex(
                                ValueError, "SQL template leakage|cannot safely isolate"
                            ):
                                await train_table_retriever(config, SimpleNamespace())
                            create_embedder.assert_not_called()
                            self.assertEqual((root / "model.json").read_bytes(), old)
                        else:
                            report = await train_table_retriever(config, SimpleNamespace())
                            self.assertTrue(report["calibrator_accepted"])
                            create_embedder.assert_called_once()
                            self.assertTrue(embedder.closed)

    async def test_split_change_during_embedding_rejects_and_keeps_old_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, bundle = training_fixture(root)
            old = (root / "model.json").read_bytes()
            embedder = SyntheticEmbedder(
                lambda: Path(config.retrieval_train_set_path).write_bytes(b"changed")
            )
            with (
                patch("text2sql.retrieval.train.OllamaEmbedder", return_value=embedder),
                patch(
                    "text2sql.retrieval.train.load_validated_knowledge_bundle", return_value=bundle
                ),
            ):
                with self.assertRaises(InputSnapshotError):
                    await train_table_retriever(config, SimpleNamespace())
            self.assertEqual((root / "model.json").read_bytes(), old)
            self.assertTrue(embedder.closed)

    async def test_aba_uses_only_original_examples_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, bundle = training_fixture(root)
            path = Path(config.retrieval_train_set_path)
            original = await asyncio.to_thread(path.read_bytes)
            expected = retrieval_dataset_fingerprint(
                config.retrieval_train_set_path,
                config.retrieval_calibration_set_path,
                config.retrieval_test_set_path,
            )
            calls = 0

            def mutate():
                nonlocal calls
                calls += 1
                path.write_bytes(b"replacement" if calls == 1 else original)

            embedder = SyntheticEmbedder(mutate)
            with (
                patch("text2sql.retrieval.train.OllamaEmbedder", return_value=embedder),
                patch(
                    "text2sql.retrieval.train.load_validated_knowledge_bundle", return_value=bundle
                ),
            ):
                report = await train_table_retriever(config, SimpleNamespace())
            self.assertTrue(report["calibrator_accepted"])
            self.assertEqual(
                PlattCalibrator.load(root / "model.json").dataset_fingerprint, expected
            )
            self.assertEqual(report["dataset_fingerprint"], expected)

    async def test_reporting_failure_cannot_publish_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, bundle = training_fixture(root)
            old = (root / "model.json").read_bytes()
            with (
                patch("text2sql.retrieval.train.OllamaEmbedder", return_value=SyntheticEmbedder()),
                patch(
                    "text2sql.retrieval.train.load_validated_knowledge_bundle", return_value=bundle
                ),
                patch("text2sql.retrieval.train.atomic_json", side_effect=OSError("report failed")),
            ):
                with self.assertRaises(OSError):
                    await train_table_retriever(config, SimpleNamespace())
            self.assertEqual((root / "model.json").read_bytes(), old)

    async def test_model_digest_change_blocks_publish_without_external_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, bundle = training_fixture(root)
            identity = {"llm": "llm-v1", "embed": "embed-v1"}
            runtime = SimpleNamespace(probe_ollama=Mock(), model_digests=lambda: dict(identity))
            old = (root / "model.json").read_bytes()
            embedder = SyntheticEmbedder(lambda: identity.update(embed="embed-v2"))
            with (
                patch("text2sql.retrieval.train.OllamaEmbedder", return_value=embedder),
                patch(
                    "text2sql.retrieval.train.load_validated_knowledge_bundle", return_value=bundle
                ),
            ):
                with self.assertRaises(InputSnapshotError):
                    await train_table_retriever(config, runtime)
            self.assertEqual((root / "model.json").read_bytes(), old)
            self.assertGreaterEqual(runtime.probe_ollama.call_count, 2)

    async def test_production_requires_pins_and_verifier_capabilities(self):
        config = replace(
            load_settings(), app_env="production", llm_model_digest="", embedding_model_digest=""
        )
        with self.assertRaisesRegex(ValueError, "configured model digest pins"):
            await _pin_models(InputSnapshot(), config, SimpleNamespace())
        config = replace(config, llm_model_digest="a" * 64, embedding_model_digest="b" * 64)
        with self.assertRaisesRegex(ValueError, "digest verification"):
            await _pin_models(InputSnapshot(), config, SimpleNamespace())

    async def test_live_schema_drift_rechecked_and_workers_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, bundle = training_fixture(root)
            config = replace(config, table_retrieval_train_schema_source="live")
            old = (root / "model.json").read_bytes()
            changed = synthetic_schema(
                {"Fact": {"columns": {"Value": {"data_type": "bigint"}}}, "Other": SCHEMA["Other"]}
            )
            initial, final = Mock(), Mock()
            initial.aclose, final.aclose = AsyncMock(), AsyncMock()
            with (
                patch("text2sql.retrieval.train.OllamaEmbedder", return_value=SyntheticEmbedder()),
                patch(
                    "text2sql.retrieval.train.load_validated_knowledge_bundle", return_value=bundle
                ),
                patch("text2sql.retrieval.train.VannaSqlExecutor", side_effect=[initial, final]),
                patch(
                    "text2sql.retrieval.train.get_live_schema",
                    new=AsyncMock(side_effect=[SCHEMA, changed]),
                ) as read,
            ):
                with self.assertRaisesRegex(InputSnapshotError, "authoritative Schema changed"):
                    await train_table_retriever(config, SimpleNamespace(sql_runner=Mock()))
            self.assertEqual(read.await_count, 2)
            self.assertTrue(all(call.kwargs["force_refresh"] for call in read.await_args_list))
            initial.aclose.assert_awaited_once()
            final.aclose.assert_awaited_once()
            self.assertEqual((root / "model.json").read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
