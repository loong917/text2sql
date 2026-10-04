"""Formal promotion is isolated from candidate work and preserves serving evidence."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.test_production_readiness import (
    SYNTHETIC_COLLECTION_EVIDENCE,
    production_fixture,
    readiness,
    synthetic_collection_evidence,
)
from text2sql.evaluation.artifact_evidence import artifact_file_hashes
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry
from text2sql.release.evidence import release_output_errors
from text2sql.release.readiness import promote_release, publication_evidence_errors
from text2sql.release.runtime_gate import ProductionReleaseGate


def promotion_fixture(root, *, first_release=False):
    config, serving, test, database = production_fixture(root)
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir, config.knowledge_active_pointer_path
    )
    approval = readiness(config, database)
    registry.release_path(serving).write_text(json.dumps(approval), encoding="utf-8")
    config = replace(
        config,
        production_release_manifest_path=str(root / "release-latest.json"),
        knowledge_db_dir=str(root / "synthetic-chroma"),
    )
    candidate = registry.candidate(schema_fingerprint=serving.schema_fingerprint)
    for source, target in (
        (serving.knowledge_index_path, candidate.knowledge_index_path),
        (serving.calibrator_path, candidate.calibrator_path),
        (serving.snapshot_path, candidate.snapshot_path),
        (serving.report_path, candidate.report_path),
        (serving.manifest_path, candidate.manifest_path),
    ):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Path(source).read_bytes())
    registry.stage(candidate)
    test["attestation"]["artifact_version"] = candidate.version
    test["attestation"]["artifact_sha256"] = artifact_file_hashes(candidate)
    registry.test_report_path(candidate).write_text(json.dumps(test), encoding="utf-8")
    if first_release:
        registry.active_pointer.unlink()
    report = readiness(config, database, selected_artifact=candidate)
    return config, registry, serving, candidate, report


class ReleasePromotionTests(unittest.TestCase):
    def runtime(self, config, *, evidence=None):
        class Runtime:
            def __init__(self, _config):
                pass

            def model_digests(self):
                return {
                    config.llm_model: config.llm_model_digest,
                    config.embedding_model: config.embedding_model_digest,
                }

            def collection_evidence(self, _collection, *, index_records=None):
                return evidence if evidence is not None else SYNTHETIC_COLLECTION_EVIDENCE

            def close(self):
                pass

        return Runtime

    def test_first_release_has_no_previous_pointer_but_can_promote(self):
        with tempfile.TemporaryDirectory() as directory:
            config, registry, _, candidate, report = promotion_fixture(
                Path(directory), first_release=True
            )
            self.assertEqual(report["status"], "ready", report["failures"])
            self.assertEqual(report["evidence"]["previous_active_pointer_sha256"], "missing")
            self.assertEqual(
                publication_evidence_errors(config, report, selected_artifact=candidate), []
            )
            with patch("text2sql.release.readiness.RuntimeResources", self.runtime(config)):
                promote_release(config, candidate, report)
            self.assertEqual(registry.load_active(), candidate)
            pointer = json.loads(registry.active_pointer.read_bytes())
            self.assertEqual(
                pointer["release_manifest_path"], str(registry.release_path(candidate))
            )
            self.assertFalse((registry.root / ".training.lock").exists())

    def test_switch_retains_old_approval_and_report_and_gate_ignores_latest_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            config, registry, serving, candidate, report = promotion_fixture(Path(directory))
            old_approval = registry.release_path(serving).read_bytes()
            old_test = registry.test_report_path(serving).read_bytes()
            self.assertEqual(report["status"], "ready", report["failures"])
            with patch("text2sql.release.readiness.RuntimeResources", self.runtime(config)):
                promote_release(config, candidate, report)
            self.assertEqual(registry.load_active(), candidate)
            self.assertEqual(registry.release_path(serving).read_bytes(), old_approval)
            self.assertEqual(registry.test_report_path(serving).read_bytes(), old_test)
            Path(config.production_release_manifest_path).write_text(
                "not evidence", encoding="utf-8"
            )
            Path(config.eval_test_report_path).write_text(
                "old test no longer used", encoding="utf-8"
            )
            with (
                patch(
                    "text2sql.release.runtime_gate.production_configuration_errors", return_value=[]
                ),
                patch("text2sql.release.runtime_gate.RuntimeResources", self.runtime(config)),
            ):
                ProductionReleaseGate(config).validate()

    def test_blocked_drifted_or_stale_candidate_does_not_touch_active(self):
        for cause in ("blocked", "dataset", "collection", "configuration", "pointer"):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as directory:
                config, registry, serving, candidate, report = promotion_fixture(Path(directory))
                old_approval = registry.release_path(serving).read_bytes()
                if cause == "blocked":
                    report["status"] = "blocked"
                    report["failures"] = ["synthetic failure"]
                elif cause == "dataset":
                    path = Path(config.eval_test_set_path)
                    path.write_bytes(path.read_bytes() + b"\n")
                elif cause == "pointer":
                    registry.active_pointer.write_bytes(
                        registry.active_pointer.read_bytes() + b"\n"
                    )
                expected_pointer = registry.active_pointer.read_bytes()
                evidence = None
                if cause == "collection":
                    evidence = {**SYNTHETIC_COLLECTION_EVIDENCE, "sha256": "a" * 64}
                elif cause == "configuration":
                    evidence = synthetic_collection_evidence(ef_search=200)
                with (
                    patch(
                        "text2sql.release.readiness.RuntimeResources",
                        self.runtime(config, evidence=evidence),
                    ),
                    self.assertRaises(ValueError),
                ):
                    promote_release(config, candidate, report)
                self.assertEqual(registry.active_pointer.read_bytes(), expected_pointer)
                self.assertEqual(registry.release_path(serving).read_bytes(), old_approval)
                self.assertFalse(registry.release_path(candidate).exists())

    def test_approved_version_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            config, registry, _, candidate, report = promotion_fixture(Path(directory))
            with patch("text2sql.release.readiness.RuntimeResources", self.runtime(config)):
                promote_release(config, candidate, report)
                pointer = registry.active_pointer.read_bytes()
                approval = registry.release_path(candidate).read_bytes()
                with self.assertRaisesRegex(ValueError, "immutable"):
                    promote_release(config, candidate, report)
            self.assertEqual(registry.active_pointer.read_bytes(), pointer)
            self.assertEqual(registry.release_path(candidate).read_bytes(), approval)

    def test_diagnostic_and_archive_paths_cannot_alias_protected_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            config, registry, serving, candidate, report = promotion_fixture(Path(directory))
            chroma = Path(config.knowledge_db_dir)
            chroma.mkdir()
            (chroma / "chroma.sqlite3").write_bytes(b"synthetic Chroma bytes")
            (chroma / "segment").mkdir()
            (chroma / "segment/index.json").write_bytes(b"synthetic segment bytes")
            protected = (
                registry.active_pointer,
                registry.candidate_pointer,
                Path(serving.snapshot_path),
                registry.release_path(serving),
                registry.test_report_path(candidate),
                Path(config.eval_test_set_path),
                Path(config.structured_knowledge_dir) / "domain/metrics.json",
                chroma / "chroma.sqlite3",
                chroma / "segment/index.json",
            )
            for field in ("production_readiness_report_path", "production_release_manifest_path"):
                for path in protected:
                    with self.subTest(field=field, path=path):
                        changed = replace(config, **{field: str(path)})
                        self.assertTrue(release_output_errors(changed, candidate))
                        original = path.read_bytes()
                        with self.assertRaisesRegex(ValueError, "unsafe release output"):
                            promote_release(changed, candidate, report)
                        self.assertEqual(path.read_bytes(), original)
            self.assertTrue(
                release_output_errors(
                    replace(
                        config,
                        production_readiness_report_path=config.production_release_manifest_path,
                    ),
                    candidate,
                )
            )

    def test_registry_version_resolution_rejects_paths_and_foreign_descriptors(self):
        with tempfile.TemporaryDirectory() as directory:
            _, registry, _, candidate, _ = promotion_fixture(Path(directory))
            for version in (
                "../active",
                "..\\active",
                "/absolute",
                "invalid",
                candidate.version + "/../",
            ):
                self.assertIsNone(registry.load_version(version))
            descriptor = Path(candidate.knowledge_index_path).parent / "artifact.json"
            value = json.loads(descriptor.read_bytes())
            value["knowledge_index_path"] = str(registry.active_pointer)
            descriptor.write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(registry.load_version(candidate.version))
