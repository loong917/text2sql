"""Release evidence must bind the exact bytes inspected, not a later file version."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.evaluation_fixture import evaluation_cases
from tests.test_production_readiness import (
    SYNTHETIC_COLLECTION_EVIDENCE,
    production_fixture,
    readiness,
)
from text2sql.core.build_info import file_sha256
from text2sql.core.exceptions import ConfigurationError
from text2sql.release.evidence import EvidenceSnapshot
from text2sql.release.readiness import publication_evidence_errors
from text2sql.release.runtime_gate import ProductionReleaseGate


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        collection = patch(
            "text2sql.release.runtime_gate.RuntimeResources.collection_evidence",
            return_value=SYNTHETIC_COLLECTION_EVIDENCE,
        )
        collection.start()
        self.addCleanup(collection.stop)

    def test_dataset_parsing_and_fingerprint_share_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.jsonl"
            content = (json.dumps(evaluation_cases()[0]) + "\n").encode()
            path.write_bytes(content)
            inputs = EvidenceSnapshot()
            self.assertEqual(inputs.sha256(path), hashlib.sha256(content).hexdigest())
            path.write_text("not valid json", encoding="utf-8")
            cases = inputs.evaluation_cases(path, split="test")
            self.assertEqual(cases[0].id, evaluation_cases()[0]["id"])
            self.assertEqual(inputs.sha256(path), hashlib.sha256(content).hexdigest())
            self.assertEqual(inputs.changed_paths(), [str(path.resolve())])

    def test_readiness_rejects_report_or_dataset_replacement_during_validation(self):
        for source in ("report", "dataset"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                config, _, _, database = production_fixture(Path(directory))
                path = Path(
                    config.eval_test_report_path
                    if source == "report"
                    else config.eval_test_set_path
                )
                original_hash = file_sha256(path)
                method = EvidenceSnapshot.json_object

                def read_then_replace(snapshot, value, method=method, config=config, path=path):
                    result = method(snapshot, value)
                    if str(value) == config.eval_test_report_path:
                        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                    return result

                with patch.object(EvidenceSnapshot, "json_object", read_then_replace):
                    report = readiness(config, database)
                self.assertEqual(report["status"], "blocked", report["failures"])
                self.assertTrue(any("inputs changed" in item for item in report["failures"]))
                field = "test_report_sha256" if source == "report" else "test_dataset_sha256"
                self.assertEqual(report["evidence"][field], original_hash)
                self.assertNotEqual(file_sha256(path), original_hash)

    def test_publication_rechecks_artifact_dataset_and_identity(self):
        for source in ("artifact", "dataset"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                config, artifact, _, database = production_fixture(Path(directory))
                report = readiness(config, database)
                self.assertEqual(publication_evidence_errors(config, report), [])
                path = Path(
                    artifact.knowledge_index_path
                    if source == "artifact"
                    else config.eval_test_set_path
                )
                path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                self.assertTrue(publication_evidence_errors(config, report))

    def test_runtime_rejects_report_replacement_after_pinned_read(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, _, database = production_fixture(Path(directory))
            report = readiness(config, database)
            Path(config.production_release_manifest_path).write_text(
                json.dumps(report), encoding="utf-8"
            )
            method = EvidenceSnapshot.read
            replaced = False

            def read_then_replace(snapshot, value):
                nonlocal replaced
                content = method(snapshot, value)
                if str(value) == config.eval_test_report_path and not replaced:
                    replaced = True
                    Path(value).write_bytes(content + b"\n")
                return content

            with (
                patch(
                    "text2sql.release.runtime_gate.production_configuration_errors", return_value=[]
                ),
                patch.object(EvidenceSnapshot, "read", read_then_replace),
                self.assertRaisesRegex(ConfigurationError, "changed during validation"),
            ):
                ProductionReleaseGate(config).validate()
