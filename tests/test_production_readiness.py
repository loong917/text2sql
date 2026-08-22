import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.core.build_info import file_sha256, release_identity
from src.core.config import load_settings
from src.core.production import production_configuration_errors
from src.infrastructure.database_preflight import DatabasePreflightReport
from src.knowledge.artifacts import KnowledgeArtifactRegistry
from src.release.readiness import build_production_readiness


class ProductionReadinessTests(unittest.TestCase):
    def test_production_secrets_are_distinct_and_session_ttl_is_bounded(self):
        shared = "s" * 32
        config = replace(
            load_settings(),
            app_env="production",
            api_key=shared,
            feedback_admin_api_key=shared,
            web_session_secret=shared,
            web_session_ttl_seconds=60,
        )

        errors = production_configuration_errors(config)

        self.assertIn("API, admin and session secrets must be pairwise distinct", errors)
        self.assertIn("WEB_SESSION_TTL_SECONDS must be between 300 and 86400", errors)

    def test_release_is_bound_to_artifact_dataset_models_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "test.jsonl"
            dataset.write_text(
                '{"should_refuse": false}\n{"should_refuse": true}\n',
                encoding="utf-8",
            )
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            artifact = registry.candidate(schema_fingerprint="schema")
            for value, content in (
                (artifact.knowledge_index_path, "[]"),
                (artifact.calibrator_path, "{}"),
                (artifact.manifest_path, "{}"),
                (artifact.report_path, "{}"),
            ):
                path = Path(value)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            registry.publish(artifact)
            report_path = root / "test-report.json"
            config = replace(
                load_settings(),
                app_env="production",
                api_key="q" * 32,
                feedback_admin_api_key="a" * 32,
                web_session_secret="s" * 32,
                web_session_cookie_secure=True,
                app_revision="commit-123",
                llm_model_digest="l" * 64,
                embedding_model_digest="e" * 64,
                sql_allowed_tables="Fact",
                sql_denied_columns="*.Secret",
                mssql_conn_str=(
                    "Driver={ODBC Driver 18 for SQL Server};Server=db;Database=test;"
                    "Encrypt=yes;TrustServerCertificate=no"
                ),
                knowledge_artifact_dir=str(root / "artifacts"),
                knowledge_active_pointer_path=str(root / "active.json"),
                eval_test_set_path=str(dataset),
                eval_test_report_path=str(report_path),
                production_eval_min_cases=1,
                production_eval_min_positive_cases=1,
                production_eval_min_refusal_cases=1,
                production_retrieval_calibration_min_cases=1,
                production_retrieval_test_min_cases=1,
            )
            report_path.write_text(
                json.dumps(
                    {
                        "split": "test",
                        "quality_gate": {"passed": True},
                        "summary": {"total": 2, "positive_total": 1, "refusal_total": 1},
                        "attestation": {
                            "artifact_version": artifact.version,
                            "schema_fingerprint": artifact.schema_fingerprint,
                            "dataset_sha256": file_sha256(dataset),
                            "release_identity": release_identity(config)
                            | {
                                "observed_model_digests": {
                                    config.llm_model: config.llm_model_digest,
                                    config.embedding_model: config.embedding_model_digest,
                                }
                            },
                        },
                        "results": [
                            {"id": "positive", "should_refuse": False, "passed": True},
                            {"id": "refusal", "should_refuse": True, "passed": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            Path(artifact.manifest_path).write_text(
                json.dumps(
                    {
                        "inputs": {"release_identity": release_identity(config)},
                        "outputs": {
                            "knowledge_index_sha256": file_sha256(artifact.knowledge_index_path),
                            "calibrator_sha256": file_sha256(artifact.calibrator_path),
                            "training_report_sha256": file_sha256(artifact.report_path),
                        },
                    }
                ),
                encoding="utf-8",
            )
            database = DatabasePreflightReport(
                success=True,
                driver="ODBC Driver 18 for SQL Server",
                database="test",
                encryption="yes",
                trust_server_certificate="no",
                read_only_principal=True,
                dangerous_permissions=(),
                configuration_errors=(),
                connection_encrypted=True,
            )

            readiness = build_production_readiness(
                config,
                database=database,
                observed_model_digests={
                    config.llm_model: config.llm_model_digest,
                    config.embedding_model: config.embedding_model_digest,
                },
            )

            self.assertEqual(readiness["status"], "ready")
            self.assertEqual(readiness["failures"], [])

            Path(artifact.knowledge_index_path).write_text('[{"tampered": true}]')
            tampered = build_production_readiness(
                config,
                database=database,
                observed_model_digests={
                    config.llm_model: config.llm_model_digest,
                    config.embedding_model: config.embedding_model_digest,
                },
            )
            self.assertIn(
                "active artifact content hash is stale: knowledge_index_sha256",
                tampered["failures"],
            )

    def test_release_rejects_small_or_stale_test_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "test.jsonl"
            dataset.write_text("{}\n", encoding="utf-8")
            report = root / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "split": "test",
                        "quality_gate": {"passed": True},
                        "summary": {"total": 1, "positive_total": 1, "refusal_total": 0},
                        "attestation": {"dataset_sha256": "stale"},
                    }
                ),
                encoding="utf-8",
            )
            config = replace(
                load_settings(),
                eval_test_set_path=str(dataset),
                eval_test_report_path=str(report),
            )
            database = DatabasePreflightReport(
                success=False,
                driver="ODBC Driver 17 for SQL Server",
                database=None,
                encryption="no",
                trust_server_certificate="yes",
                read_only_principal=None,
                dangerous_permissions=(),
                configuration_errors=("driver",),
                error_code="08001",
            )

            readiness = build_production_readiness(
                config,
                database=database,
                observed_model_digests={},
            )

            self.assertEqual(readiness["status"], "blocked")
            self.assertTrue(any("production minimum" in item for item in readiness["failures"]))
            self.assertIn("frozen test dataset fingerprint is stale", readiness["failures"])
