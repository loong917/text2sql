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
from text2sql.api.server import create_app
from text2sql.core.build_info import file_sha256
from text2sql.core.exceptions import ConfigurationError
from text2sql.release.runtime_gate import ProductionReleaseGate


class ProductionRuntimeGateTests(unittest.TestCase):
    def setUp(self):
        # The gate contract is tested with small isolated datasets, not production policy floors.
        policy = patch(
            "text2sql.release.runtime_gate.production_configuration_errors", return_value=[]
        )
        policy.start()
        self.addCleanup(policy.stop)
        collection = patch(
            "text2sql.release.runtime_gate.RuntimeResources.collection_evidence",
            return_value=SYNTHETIC_COLLECTION_EVIDENCE,
        )
        collection.start()
        self.addCleanup(collection.stop)

    def release_fixture(self, root):
        config, artifact, _, database = production_fixture(root)
        report = readiness(config, database)
        self.assertEqual(report["status"], "ready", report["failures"])
        Path(config.production_release_manifest_path).write_text(
            json.dumps(report), encoding="utf-8"
        )
        return config, artifact

    def test_valid_release_passes_without_online_evaluation_source_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _ = self.release_fixture(Path(directory))
            Path(config.eval_test_set_path).unlink()
            gate = ProductionReleaseGate(config)
            gate.validate()
            gate.validate()

    def test_runtime_rechecks_collection_on_every_gate_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _ = self.release_fixture(Path(directory))
            changed = {**SYNTHETIC_COLLECTION_EVIDENCE, "sha256": "f" * 64}
            with patch(
                "text2sql.release.runtime_gate.RuntimeResources.collection_evidence",
                side_effect=[SYNTHETIC_COLLECTION_EVIDENCE, changed],
            ) as probe:
                gate = ProductionReleaseGate(config)
                gate.validate()
                with self.assertRaisesRegex(ConfigurationError, "collection content changed"):
                    gate.validate()
                self.assertEqual(probe.call_count, 2)

    def test_runtime_rejects_collection_configuration_drift_after_successful_validation(self):
        for changed in (
            synthetic_collection_evidence(ef_search=200),
            synthetic_collection_evidence(embedding_model="other-embedding-model"),
            synthetic_collection_evidence(collection_metadata={"hnsw:search_ef": 200}),
        ):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                config, _ = self.release_fixture(Path(directory))
                with patch(
                    "text2sql.release.runtime_gate.RuntimeResources.collection_evidence",
                    side_effect=[SYNTHETIC_COLLECTION_EVIDENCE, changed],
                ) as probe:
                    gate = ProductionReleaseGate(config)
                    gate.validate()
                    with self.assertRaisesRegex(ConfigurationError, "collection content changed"):
                        gate.validate()
                    self.assertEqual(probe.call_count, 2)

    def test_global_latest_report_and_approval_are_not_runtime_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = self.release_fixture(root)
            latest_report = root / "latest-test.json"
            latest_approval = root / "latest-approval.json"
            latest_report.write_text('{"quality_gate":{"passed":false}}', encoding="utf-8")
            latest_approval.write_text('{"status":"blocked"}', encoding="utf-8")
            runtime_config = replace(
                config,
                eval_test_report_path=str(latest_report),
                production_release_manifest_path=str(latest_approval),
            )
            ProductionReleaseGate(runtime_config).validate()

    def test_same_version_artifact_mutation_and_report_replacement_are_rejected(self):
        for mutation in ("artifact", "report"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                config, artifact = self.release_fixture(Path(directory))
                gate = ProductionReleaseGate(config)
                gate.validate()
                path = Path(
                    artifact.knowledge_index_path
                    if mutation == "artifact"
                    else config.eval_test_report_path
                )
                path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ConfigurationError, "content changed|report changed"):
                    gate.validate()

    def test_access_policy_or_database_principal_change_invalidates_release(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _ = self.release_fixture(Path(directory))
            for changed in (
                replace(config, sql_allowed_tables="Fact,Other"),
                replace(config, mssql_conn_str=config.mssql_conn_str + ";UID=other_user"),
            ):
                with (
                    self.subTest(changed=changed.sql_allowed_tables),
                    self.assertRaisesRegex(ConfigurationError, "identity changed"),
                ):
                    ProductionReleaseGate(changed).validate()

    def test_production_api_creation_requires_a_release_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, _, _ = production_fixture(Path(directory))
            self.assertFalse(Path(config.production_release_manifest_path).exists())
            with (
                patch("text2sql.api.server.production_configuration_errors", return_value=[]),
                self.assertRaisesRegex(ConfigurationError, "发布证据失效"),
            ):
                create_app(config=config)

    def test_runtime_rejects_semantic_check_subsets_without_original_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _ = self.release_fixture(Path(directory))
            Path(config.eval_test_set_path).unlink()
            report_path = Path(config.eval_test_report_path)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["results"][0]["checks"] = [
                item
                for item in report["results"][0]["checks"]
                if not item["name"].startswith("semantic_ir:")
                or item["name"] == "semantic_ir:version"
            ]
            report_path.write_text(json.dumps(report), encoding="utf-8")
            release_path = Path(config.production_release_manifest_path)
            release = json.loads(release_path.read_text(encoding="utf-8"))
            release["evidence"]["test_report_sha256"] = file_sha256(report_path)
            release_path.write_text(json.dumps(release), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "complete current semantic checks"):
                ProductionReleaseGate(config).validate()
