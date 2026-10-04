import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.catalog_fixture import TEST_CATALOG
from tests.evaluation_fixture import evaluation_cases, evaluation_results, plan_diagnostics
from tests.test_production_readiness import (
    SYNTHETIC_COLLECTION_EVIDENCE,
    production_fixture,
    synthetic_collection_evidence,
)
from text2sql.core.build_info import file_sha256
from text2sql.evaluation.dataset import load_evaluation_cases_bytes
from text2sql.evaluation.report_contract import validate_evaluation_report
from text2sql.evaluation.run import main
from text2sql.evaluation.service import evaluate_case
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry


class FrozenEvaluationRunnerTests(unittest.TestCase):
    def run_fixture(self, root, mutate=None, *, candidate=False):
        config, artifact, _, _ = production_fixture(root)
        active = artifact
        if candidate:
            registry = KnowledgeArtifactRegistry(
                config.knowledge_artifact_dir, config.knowledge_active_pointer_path
            )
            selected = registry.candidate(schema_fingerprint=artifact.schema_fingerprint)
            for original, destination in (
                (artifact.knowledge_index_path, selected.knowledge_index_path),
                (artifact.calibrator_path, selected.calibrator_path),
                (artifact.snapshot_path, selected.snapshot_path),
                (artifact.manifest_path, selected.manifest_path),
                (artifact.report_path, selected.report_path),
            ):
                path = Path(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Path(original).read_bytes())
            registry.stage(selected)
            artifact = selected
        dataset_bytes = Path(config.eval_test_set_path).read_bytes()
        memory = object()

        class Runtime:
            closed = False
            evidence = SYNTHETIC_COLLECTION_EVIDENCE

            def probe_ollama(self):
                pass

            def model_digests(self):
                return {
                    config.llm_model: config.llm_model_digest,
                    config.embedding_model: config.embedding_model_digest,
                }

            def collection_evidence(self, collection_name, *, index_records=None):
                return self.evidence

            def create_knowledge_memory(self, *, collection_name):
                self.collection_name = collection_name
                return memory

            def close(self):
                self.closed = True

        runtime = Runtime()
        runtime.serving_artifact = active

        async def evaluate(settings, resources, split, **kwargs):
            self.assertIs(resources, runtime)
            self.assertEqual(split, "test")
            self.assertIs(kwargs["knowledge_memory"], memory)
            self.assertEqual(kwargs["dataset_bytes"], dataset_bytes)
            runtime.frozen_index_path = kwargs["knowledge_index_path"]
            self.assertNotEqual(kwargs["knowledge_index_path"], artifact.knowledge_index_path)
            self.assertEqual(
                await asyncio.to_thread(Path(kwargs["knowledge_index_path"]).read_bytes),
                await asyncio.to_thread(Path(artifact.knowledge_index_path).read_bytes),
            )
            self.assertEqual(
                await asyncio.to_thread(Path(kwargs["calibrator_path"]).read_bytes),
                await asyncio.to_thread(Path(artifact.calibrator_path).read_bytes),
            )
            if mutate:
                mutate(config, artifact)
            return evaluation_results(evaluation_cases())

        return config, artifact, runtime, evaluate

    def invoke(self, config, runtime, evaluate, *arguments):
        with (
            patch("sys.argv", ["evaluate", *arguments]),
            patch("text2sql.core.config.load_settings", return_value=config),
            patch("text2sql.infrastructure.runtime.RuntimeResources", return_value=runtime),
            patch("text2sql.evaluation.wiring.run_configured_evaluation", side_effect=evaluate),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            main()

    def test_candidate_test_is_version_owned_and_does_not_switch_serving_version(self):
        with tempfile.TemporaryDirectory() as directory:
            config, candidate, runtime, evaluate = self.run_fixture(Path(directory), candidate=True)
            pointer_before = Path(config.knowledge_active_pointer_path).read_bytes()
            report_before = Path(config.eval_test_report_path).read_bytes()
            self.invoke(
                config,
                runtime,
                evaluate,
                "--split",
                "test",
                "--artifact",
                candidate.version,
                "--enforce-gate",
            )
            report = json.loads(KnowledgeArtifactRegistry.test_report_path(candidate).read_bytes())
            self.assertEqual(report["attestation"]["artifact_version"], candidate.version)
            self.assertEqual(
                Path(config.knowledge_active_pointer_path).read_bytes(), pointer_before
            )
            self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)
            self.assertEqual(runtime.collection_name, candidate.collection_name)
            self.assertTrue(runtime.closed)

    def test_failed_candidate_test_keeps_current_version_and_its_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            config, candidate, runtime, evaluate = self.run_fixture(Path(directory), candidate=True)
            pointer_before = Path(config.knowledge_active_pointer_path).read_bytes()
            report_before = Path(config.eval_test_report_path).read_bytes()

            async def failed_evaluation(*args, **kwargs):
                results = await evaluate(*args, **kwargs)
                case = evaluation_cases()[0]
                results[0] = evaluate_case(
                    case,
                    {
                        "success": True,
                        "outcome": "success",
                        "sql": case["baseline_sql"],
                        "result": [{"Total": 2}],
                        "result_columns": ["Total"],
                        "result_row_count": 1,
                        "candidate_tables": ["Fact"],
                        "diagnostics": plan_diagnostics(case["question"]),
                    },
                    TEST_CATALOG,
                    {
                        "success": True,
                        "rows": [{"Total": 1}],
                        "columns": ["Total"],
                        "row_count": 1,
                        "error": None,
                    },
                ) | {"case_index": 1}
                return results

            with self.assertRaises(SystemExit) as error:
                self.invoke(
                    config,
                    runtime,
                    failed_evaluation,
                    "--split",
                    "test",
                    "--artifact",
                    candidate.version,
                    "--enforce-gate",
                )
            self.assertEqual(error.exception.code, 1)
            candidate_report = json.loads(
                KnowledgeArtifactRegistry.test_report_path(candidate).read_bytes()
            )
            self.assertFalse(candidate_report["quality_gate"]["passed"])
            self.assertEqual(
                Path(config.knowledge_active_pointer_path).read_bytes(), pointer_before
            )
            self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)
            self.assertTrue(runtime.closed)

    def test_candidate_cannot_redirect_output_over_current_report_or_other_version(self):
        with tempfile.TemporaryDirectory() as directory:
            config, candidate, runtime, evaluate = self.run_fixture(Path(directory), candidate=True)
            report_before = Path(config.eval_test_report_path).read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.invoke(
                    config,
                    runtime,
                    evaluate,
                    "--split",
                    "test",
                    "--artifact",
                    candidate.version,
                    "--output",
                    config.eval_test_report_path,
                )
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)
            self.assertFalse(KnowledgeArtifactRegistry.test_report_path(candidate).exists())

    def test_approved_active_test_evidence_cannot_be_overwritten_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            config, artifact, runtime, evaluate = self.run_fixture(Path(directory))
            KnowledgeArtifactRegistry.release_path(artifact).write_text(
                '{"status":"ready"}', encoding="utf-8"
            )
            report_before = Path(config.eval_test_report_path).read_bytes()
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "approved artifacts are immutable"),
            ):
                self.invoke(
                    config,
                    runtime,
                    evaluate,
                    "--split",
                    "test",
                    "--artifact",
                    artifact.version,
                    "--output",
                    config.eval_test_report_path,
                )
            self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)

    def test_candidate_collection_drift_blocks_new_test_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config, candidate, runtime, evaluate = self.run_fixture(Path(directory), candidate=True)
            pointer_before = Path(config.knowledge_active_pointer_path).read_bytes()
            report_before = Path(config.eval_test_report_path).read_bytes()

            async def changed_collection(*args, **kwargs):
                result = await evaluate(*args, **kwargs)
                runtime.evidence = {**SYNTHETIC_COLLECTION_EVIDENCE, "sha256": "a" * 64}
                return result

            with self.assertRaisesRegex(ValueError, "collection content changed"):
                self.invoke(
                    config,
                    runtime,
                    changed_collection,
                    "--split",
                    "test",
                    "--artifact",
                    candidate.version,
                )
            self.assertFalse(KnowledgeArtifactRegistry.test_report_path(candidate).exists())
            self.assertEqual(
                Path(config.knowledge_active_pointer_path).read_bytes(), pointer_before
            )
            self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)
            self.assertTrue(runtime.closed)

    def test_collection_configuration_drift_cannot_publish_test_evidence(self):
        for stage in ("before_evaluation", "before_publication"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                config, candidate, runtime, evaluate = self.run_fixture(
                    Path(directory), candidate=True
                )
                pointer_before = Path(config.knowledge_active_pointer_path).read_bytes()
                report_before = Path(config.eval_test_report_path).read_bytes()
                changed = synthetic_collection_evidence(ef_search=200)

                async def change_configuration(
                    *args, evaluate=evaluate, runtime=runtime, changed=changed, **kwargs
                ):
                    results = await evaluate(*args, **kwargs)
                    runtime.evidence = changed
                    return results

                if stage == "before_evaluation":
                    runtime.evidence = changed
                with self.assertRaisesRegex(ValueError, "collection content changed"):
                    self.invoke(
                        config,
                        runtime,
                        change_configuration,
                        "--split",
                        "test",
                        "--artifact",
                        candidate.version,
                    )
                self.assertFalse(KnowledgeArtifactRegistry.test_report_path(candidate).exists())
                self.assertEqual(
                    Path(config.knowledge_active_pointer_path).read_bytes(), pointer_before
                )
                self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)
                self.assertTrue(runtime.closed)

    def test_model_drift_at_each_final_guard_keeps_previous_test_evidence(self):
        for stage in ("after_evaluation", "before_publication"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                config, candidate, runtime, evaluate = self.run_fixture(
                    Path(directory), candidate=True
                )
                pointer_before = Path(config.knowledge_active_pointer_path).read_bytes()
                report_before = Path(config.eval_test_report_path).read_bytes()
                stable = runtime.model_digests()
                changed = {**stable, config.embedding_model: "f" * 64}
                observations = [stable, changed]
                if stage == "before_publication":
                    observations = [stable, stable, changed]
                with (
                    patch.object(runtime, "model_digests", side_effect=observations),
                    self.assertRaisesRegex(RuntimeError, "model identities changed"),
                ):
                    self.invoke(
                        config,
                        runtime,
                        evaluate,
                        "--split",
                        "test",
                        "--artifact",
                        candidate.version,
                    )
                self.assertFalse(KnowledgeArtifactRegistry.test_report_path(candidate).exists())
                self.assertEqual(
                    Path(config.knowledge_active_pointer_path).read_bytes(), pointer_before
                )
                self.assertEqual(Path(config.eval_test_report_path).read_bytes(), report_before)
                self.assertTrue(runtime.closed)
                registry = KnowledgeArtifactRegistry(
                    config.knowledge_artifact_dir, config.knowledge_active_pointer_path
                )
                lease = registry.acquire_training_lease(
                    stale_seconds=config.knowledge_training_lock_stale_seconds
                )
                lease.release()

    def test_evaluation_cannot_run_while_shared_publication_lease_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            config, candidate, runtime, evaluate = self.run_fixture(Path(directory), candidate=True)
            registry = KnowledgeArtifactRegistry(
                config.knowledge_artifact_dir, config.knowledge_active_pointer_path
            )
            lease = registry.acquire_training_lease(
                stale_seconds=config.knowledge_training_lock_stale_seconds
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    self.invoke(
                        config,
                        runtime,
                        evaluate,
                        "--split",
                        "test",
                        "--artifact",
                        candidate.version,
                    )
                self.assertFalse(KnowledgeArtifactRegistry.test_report_path(candidate).exists())
                self.assertFalse(hasattr(runtime, "collection_name"))
                self.assertFalse(runtime.closed)
            finally:
                lease.release()

    def test_runner_attests_the_artifact_that_was_actually_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            config, artifact, runtime, evaluate = self.run_fixture(Path(directory))
            with (
                patch("sys.argv", ["evaluate", "--split", "test", "--enforce-gate"]),
                patch("text2sql.core.config.load_settings", return_value=config),
                patch("text2sql.infrastructure.runtime.RuntimeResources", return_value=runtime),
                patch("text2sql.evaluation.wiring.run_configured_evaluation", side_effect=evaluate),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main()
            report = json.loads(Path(config.eval_test_report_path).read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["attestation"]["artifact_version"], artifact.version)
            self.assertEqual(
                report["attestation"]["artifact_sha256"]["knowledge_snapshot_sha256"],
                file_sha256(artifact.snapshot_path),
            )
            self.assertTrue(runtime.closed)
            self.assertFalse(Path(runtime.frozen_index_path).parent.exists())
            self.assertEqual(runtime.collection_name, artifact.collection_name)

    def test_changed_dataset_or_artifact_does_not_publish_new_evidence(self):
        for source in ("dataset", "artifact", "pointer"):

            def mutate(config, artifact, source=source):
                if source == "dataset":
                    path = Path(config.eval_test_set_path)
                    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                elif source == "artifact":
                    path = Path(artifact.snapshot_path)
                    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                else:
                    Path(config.knowledge_active_pointer_path).write_text("{}", encoding="utf-8")

            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                config, _, runtime, evaluate = self.run_fixture(Path(directory), mutate)
                original_hash = file_sha256(config.eval_test_report_path)
                with (
                    patch("sys.argv", ["evaluate", "--split", "test"]),
                    patch("text2sql.core.config.load_settings", return_value=config),
                    patch("text2sql.infrastructure.runtime.RuntimeResources", return_value=runtime),
                    patch(
                        "text2sql.evaluation.wiring.run_configured_evaluation", side_effect=evaluate
                    ),
                    self.assertRaisesRegex(RuntimeError, "changed"),
                ):
                    main()
                self.assertEqual(file_sha256(config.eval_test_report_path), original_hash)
                self.assertTrue(runtime.closed)
                self.assertFalse(Path(runtime.frozen_index_path).parent.exists())

    def test_aba_source_replacement_cannot_change_consumed_bytes(self):
        for source in ("dataset", "artifact"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                config, artifact, runtime, _ = self.run_fixture(Path(directory))
                frozen_paths = []

                async def evaluate(
                    settings,
                    resources,
                    split,
                    *,
                    source=source,
                    config=config,
                    artifact=artifact,
                    frozen_paths=frozen_paths,
                    **kwargs,
                ):
                    path = Path(
                        config.eval_test_set_path
                        if source == "dataset"
                        else artifact.knowledge_index_path
                    )
                    original = await asyncio.to_thread(path.read_bytes)
                    replacement = original + b"\n" if source == "dataset" else b'["tampered"]'
                    try:
                        await asyncio.to_thread(path.write_bytes, replacement)
                        consumed = (
                            kwargs["dataset_bytes"]
                            if source == "dataset"
                            else await asyncio.to_thread(
                                Path(kwargs["knowledge_index_path"]).read_bytes
                            )
                        )
                        self.assertEqual(consumed, original)
                        self.assertNotEqual(consumed, await asyncio.to_thread(path.read_bytes))
                        cases = load_evaluation_cases_bytes(
                            kwargs["dataset_bytes"],
                            source=config.eval_test_set_path,
                            expected_split="test",
                        )
                        frozen_paths.append(Path(kwargs["knowledge_index_path"]).parent)
                        return evaluation_results([case.payload for case in cases])
                    finally:
                        await asyncio.to_thread(path.write_bytes, original)

                with (
                    patch("sys.argv", ["evaluate", "--split", "test", "--enforce-gate"]),
                    patch("text2sql.core.config.load_settings", return_value=config),
                    patch("text2sql.infrastructure.runtime.RuntimeResources", return_value=runtime),
                    patch(
                        "text2sql.evaluation.wiring.run_configured_evaluation", side_effect=evaluate
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    main()
                self.assertTrue(runtime.closed)
                self.assertFalse(frozen_paths[0].exists())
                report = json.loads(Path(config.eval_test_report_path).read_text(encoding="utf-8"))
                self.assertEqual(
                    report["attestation"]["dataset_sha256"], file_sha256(config.eval_test_set_path)
                )

    def test_source_change_during_report_validation_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, runtime, evaluate = self.run_fixture(Path(directory))
            original_hash = file_sha256(config.eval_test_report_path)

            def validate_then_replace(*args, **kwargs):
                result = validate_evaluation_report(*args, **kwargs)
                path = Path(config.eval_test_set_path)
                path.write_bytes(path.read_bytes() + b"\n")
                return result

            with (
                patch("sys.argv", ["evaluate", "--split", "test"]),
                patch("text2sql.core.config.load_settings", return_value=config),
                patch("text2sql.infrastructure.runtime.RuntimeResources", return_value=runtime),
                patch("text2sql.evaluation.wiring.run_configured_evaluation", side_effect=evaluate),
                patch(
                    "text2sql.evaluation.report_contract.validate_evaluation_report",
                    side_effect=validate_then_replace,
                ),
                self.assertRaisesRegex(RuntimeError, "before publication"),
            ):
                main()
            self.assertEqual(file_sha256(config.eval_test_report_path), original_hash)
            self.assertTrue(runtime.closed)
