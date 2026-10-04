import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from tests.evaluation_fixture import (
    EVALUATION_SCHEMA,
    evaluation_cases,
    evaluation_results,
    evaluation_split_cases,
    synthetic_review,
)
from text2sql.core.build_info import file_sha256, release_identity
from text2sql.core.config import load_settings
from text2sql.core.production import production_configuration_errors
from text2sql.domain.sql_validation import SqlSafetyPolicy
from text2sql.evaluation.artifact_evidence import artifact_file_hashes
from text2sql.evaluation.gold_set import GoldCase, export_gold_set
from text2sql.evaluation.report_contract import EVALUATION_REPORT_VERSION
from text2sql.evaluation.reporting import evaluate_configured_quality_gate, summarize_evaluation
from text2sql.infrastructure.database_preflight import DatabasePreflightReport
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry
from text2sql.knowledge.collection_evidence import build_collection_evidence
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.snapshot import ArtifactSnapshot
from text2sql.knowledge.structured import load_validated_knowledge_bundle
from text2sql.release.readiness import build_production_readiness


def synthetic_collection_evidence(
    documents=(),
    *,
    index_records=None,
    ef_search=100,
    embedding_model="nomic-embed-text",
    collection_metadata=None,
):
    """Pure-memory vectors verify evidence contracts, never actual Chroma quality."""
    texts = list(documents)
    return build_collection_evidence(
        {
            "ids": [f"synthetic-{index}" for index in range(len(texts))],
            "documents": texts,
            "metadatas": [
                {"content": text, "timestamp": "2026-10-03T00:00:00+00:00", "is_text_memory": True}
                for text in texts
            ],
            "embeddings": [[1.0, 0.0] for _ in texts],
        },
        configuration={
            "hnsw": {"space": "cosine", "ef_search": ef_search},
            "spann": None,
            "embedding_function": {
                "type": "known",
                "name": "ollama",
                "config": {
                    "url": "http://localhost:11434/api/embeddings",
                    "model_name": embedding_model,
                    "timeout": 120,
                },
            },
        },
        collection_metadata=collection_metadata,
        index_records=index_records,
    )


SYNTHETIC_COLLECTION_EVIDENCE = synthetic_collection_evidence()


def production_fixture(root: Path):
    cases = evaluation_cases()
    schema = EVALUATION_SCHEMA
    source = root / "reviewed-source"
    (source / "schema").mkdir(parents=True)
    (source / "domain").mkdir()
    (source / "manifest.json").write_text('{"schema_version":1}', encoding="utf-8")
    card = {
        "table": "Fact",
        "description": "synthetic event table",
        "grain": "one synthetic event per row",
    }
    card["review"] = synthetic_review(schema, content=card)
    (source / "schema/table_cards.jsonl").write_text(
        json.dumps(card) + "\n",
        encoding="utf-8",
    )
    for name in ("dimensions", "joins", "policies", "entities"):
        (source / f"domain/{name}.json").write_text("[]", encoding="utf-8")
    metric = {
        "id": "fixture_count",
        "name": "synthetic count",
        "source_table": "Fact",
        "column": "*",
        "aggregation": "COUNT",
        "unit": "synthetic rows",
    }
    metric["review"] = synthetic_review(schema, content=metric)
    (source / "domain/metrics.json").write_text(
        json.dumps([metric]),
        encoding="utf-8",
    )
    canonical = root / "canonical"
    canonical.mkdir()
    gold_cases = []
    for split in ("dev", "test", "retrieval_train", "retrieval_calibration", "retrieval_test"):
        for case in cases if split == "test" else evaluation_split_cases(split):
            gold_cases.append(
                GoldCase(
                    id=f"canonical-{split}-{case['id']}",
                    question=case["question"],
                    purpose=split,
                    query_family_id=case["template_id"],
                    category="synthetic",
                    difficulty="easy",
                    source="synthetic",
                    source_line=1,
                    source_sha256="a" * 64,
                    status="approved",
                    payload=case,
                    review=case["review"],
                    execution=case.get("execution"),
                )
            )
    (canonical / "cases.jsonl").write_text(
        "".join(case.model_dump_json() + "\n" for case in gold_cases), encoding="utf-8"
    )
    generation = export_gold_set(
        canonical, root / "generation", schema, SqlSafetyPolicy(), knowledge_root=source
    )
    dataset = generation / "evaluation/test.jsonl"
    registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
    artifact = registry.candidate(schema_fingerprint=schema_fingerprint(schema))
    bundle = load_validated_knowledge_bundle(
        generation / "knowledge", schema, require_reviewed=True
    )
    for value, content in (
        (artifact.knowledge_index_path, "[]"),
        (artifact.calibrator_path, "{}"),
        (artifact.manifest_path, "{}"),
        (artifact.report_path, '{"quality_gate":{"passed":true}}'),
        (
            artifact.snapshot_path,
            json.dumps(ArtifactSnapshot(schema, bundle, "fixture-dataset").to_dict()),
        ),
    ):
        path = Path(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    registry.stage(artifact)
    registry.publish(artifact)  # Explicitly simulate an existing serving version.
    report_path = registry.test_report_path(artifact)
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
        mssql_conn_str="Driver={ODBC Driver 18 for SQL Server};Server=db;Database=test;Encrypt=yes;TrustServerCertificate=no",
        knowledge_artifact_dir=str(root / "artifacts"),
        knowledge_active_pointer_path=str(root / "active.json"),
        knowledge_db_dir=str(root / "synthetic-chroma"),
        table_retrieval_calibrator_path=str(root / "source-calibrator.json"),
        feedback_db_path=str(root / "synthetic-feedback.sqlite"),
        schema_snapshot_path=str(root / "source-schema.json"),
        training_state_path=str(root / "source-training-state.json"),
        eval_test_set_path=str(dataset),
        eval_dev_set_path=str(generation / "evaluation/dev.jsonl"),
        retrieval_train_set_path=str(generation / "evaluation/retrieval_train.jsonl"),
        retrieval_calibration_set_path=str(generation / "evaluation/retrieval_calibration.jsonl"),
        retrieval_test_set_path=str(generation / "evaluation/retrieval_test.jsonl"),
        structured_knowledge_dir=str(generation / "knowledge"),
        eval_test_report_path=str(report_path),
        production_readiness_report_path=str(root / "production-readiness.json"),
        production_release_manifest_path=str(registry.release_path(artifact)),
        production_eval_min_cases=2,
        production_eval_min_positive_cases=1,
        production_eval_min_refusal_cases=1,
        production_retrieval_calibration_min_cases=1,
        production_retrieval_test_min_cases=1,
        eval_min_cases=2,
        eval_min_positive_cases=1,
        eval_min_refusal_cases=1,
    )
    results = evaluation_results(cases)
    summary = summarize_evaluation(results)
    report = {
        "schema_version": EVALUATION_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "split": "test",
        "quality_gate": evaluate_configured_quality_gate(summary, config),
        "summary": summary,
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
        "results": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    Path(artifact.manifest_path).write_text(
        json.dumps(
            {
                "inputs": {"release_identity": release_identity(config)},
                "outputs": {
                    "knowledge_index_sha256": file_sha256(artifact.knowledge_index_path),
                    "calibrator_sha256": file_sha256(artifact.calibrator_path),
                    "training_report_sha256": file_sha256(artifact.report_path),
                    "knowledge_snapshot_sha256": file_sha256(artifact.snapshot_path),
                    "collection_evidence": SYNTHETIC_COLLECTION_EVIDENCE,
                },
            }
        ),
        encoding="utf-8",
    )
    report["attestation"]["artifact_sha256"] = artifact_file_hashes(artifact)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
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
    return config, artifact, report, database


def readiness(config, database, **overrides):
    # Small fixtures exercise evidence contracts; production policy floors remain enforced.
    options = {
        "knowledge_collection_ready": True,
        "calibrator_ready": True,
        "observed_collection_evidence": SYNTHETIC_COLLECTION_EVIDENCE,
        **overrides,
    }
    with patch("text2sql.release.readiness.production_configuration_errors", return_value=[]):
        return build_production_readiness(
            config,
            database=database,
            observed_model_digests={
                config.llm_model: config.llm_model_digest,
                config.embedding_model: config.embedding_model_digest,
            },
            **options,
        )


class ProductionReadinessTests(unittest.TestCase):
    def test_collection_configuration_is_bound_to_the_frozen_test_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            config, artifact, _, database = production_fixture(Path(directory))
            changed = synthetic_collection_evidence(ef_search=200)
            manifest_path = Path(artifact.manifest_path)
            manifest = json.loads(manifest_path.read_bytes())
            manifest["outputs"]["collection_evidence"] = changed
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            decision = readiness(config, database, observed_collection_evidence=changed)
            self.assertEqual(decision["status"], "blocked")
            self.assertIn(
                "frozen test artifact content fingerprints are stale", decision["failures"]
            )

    def test_legacy_collection_evidence_cannot_authorize_a_production_release(self):
        with tempfile.TemporaryDirectory() as directory:
            config, artifact, _, database = production_fixture(Path(directory))
            legacy = {
                key: value
                for key, value in SYNTHETIC_COLLECTION_EVIDENCE.items()
                if key != "configuration_sha256"
            }
            legacy["schema_version"] = 1
            manifest_path = Path(artifact.manifest_path)
            manifest = json.loads(manifest_path.read_bytes())
            manifest["outputs"]["collection_evidence"] = legacy
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            decision = readiness(config, database, observed_collection_evidence=legacy)
            self.assertEqual(decision["status"], "blocked")
            self.assertTrue(
                any("collection evidence invalid" in item for item in decision["failures"])
            )

    def test_readiness_rejects_collection_configuration_drift_without_record_changes(self):
        for changed in (
            synthetic_collection_evidence(ef_search=200),
            synthetic_collection_evidence(embedding_model="other-embedding-model"),
            synthetic_collection_evidence(collection_metadata={"hnsw:search_ef": 200}),
        ):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                config, _, _, database = production_fixture(Path(directory))
                self.assertEqual(changed["count"], SYNTHETIC_COLLECTION_EVIDENCE["count"])
                self.assertEqual(
                    changed["embedding_dimensions"],
                    SYNTHETIC_COLLECTION_EVIDENCE["embedding_dimensions"],
                )
                self.assertNotEqual(
                    changed["configuration_sha256"],
                    SYNTHETIC_COLLECTION_EVIDENCE["configuration_sha256"],
                )
                decision = readiness(config, database, observed_collection_evidence=changed)
                self.assertEqual(decision["status"], "blocked")
                self.assertTrue(any("collection" in item for item in decision["failures"]))

    def test_readiness_rejects_actual_collection_content_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, _, database = production_fixture(Path(directory))
            report = readiness(
                config,
                database,
                observed_collection_evidence={
                    **SYNTHETIC_COLLECTION_EVIDENCE,
                    "sha256": "f" * 64,
                },
            )
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(any("collection" in item for item in report["failures"]))

    def test_frozen_positive_cases_cannot_opt_out_of_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, report, database = production_fixture(Path(directory))
            cases = evaluation_cases()
            cases[0]["must_execute"] = False
            Path(config.eval_test_set_path).write_text(
                "\n".join(json.dumps(case) for case in cases) + "\n", encoding="utf-8"
            )
            report["attestation"]["dataset_sha256"] = file_sha256(config.eval_test_set_path)
            Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
            decision = readiness(config, database)
            self.assertEqual(decision["status"], "blocked")
            self.assertTrue(
                any(
                    "frozen test dataset is missing or invalid" in item
                    for item in decision["failures"]
                ),
                decision["failures"],
            )

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
            config, artifact, _, database = production_fixture(Path(directory))
            report = readiness(config, database)
            self.assertEqual(report["status"], "ready", report["failures"])
            self.assertEqual(
                report["evidence"]["test_report_sha256"], file_sha256(config.eval_test_report_path)
            )
            self.assertEqual(
                report["evidence"]["artifact_sha256"]["knowledge_snapshot_sha256"],
                file_sha256(artifact.snapshot_path),
            )
            Path(artifact.knowledge_index_path).write_text('[{"tampered": true}]', encoding="utf-8")
            self.assertIn(
                "active artifact content hash is stale: knowledge_index_sha256",
                readiness(config, database)["failures"],
            )

    def test_release_rejects_empty_results_even_when_passed_flag_and_counts_are_forged(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, report, database = production_fixture(Path(directory))
            report["results"] = []
            report["quality_gate"]["passed"] = True
            Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
            decision = readiness(config, database)
            self.assertEqual(decision["status"], "blocked")
            self.assertTrue(any("schema invalid" in item for item in decision["failures"]))

    def test_release_rejects_missing_duplicate_and_unknown_case_ids(self):
        for mutation in ("missing", "duplicate", "unknown"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                config, _, report, database = production_fixture(Path(directory))
                if mutation == "missing":
                    report["results"].pop()
                elif mutation == "duplicate":
                    report["results"][1] = dict(report["results"][0])
                else:
                    report["results"][1]["id"] = "not-in-dataset"
                Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
                failures = readiness(config, database)["failures"]
                self.assertTrue(any("every frozen case ID" in item for item in failures), failures)

    def test_release_recomputes_summary_and_current_quality_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, report, database = production_fixture(Path(directory))
            report["summary"]["pass_rate"] = 0.0
            report["quality_gate"]["thresholds"]["min_pass_rate"] = 0.0
            Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
            failures = readiness(config, database)["failures"]
            self.assertIn("frozen test summary contradicts per-case evidence", failures)
            self.assertIn(
                "frozen test reported gate differs from current recomputed thresholds", failures
            )

    def test_release_rejects_missing_checks_and_string_booleans(self):
        for mutation in ("missing-check", "string-bool", "summary-bool", "truncated"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                config, _, report, database = production_fixture(Path(directory))
                result = report["results"][0]
                if mutation == "missing-check":
                    result["checks"] = [
                        item for item in result["checks"] if item["name"] != "baseline_result_match"
                    ]
                elif mutation == "string-bool":
                    result["passed"] = "true"
                elif mutation == "summary-bool":
                    report["summary"]["passed"] = True
                else:
                    result["result_truncated"] = True
                Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
                decision = readiness(config, database)
                self.assertEqual(decision["status"], "blocked", mutation)

    def test_release_rejects_invalid_nested_report_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, report, database = production_fixture(Path(directory))
            report["attestation"] = []
            Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(readiness(config, database)["status"], "blocked")

    def test_release_rejects_small_or_stale_test_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, report, database = production_fixture(Path(directory))
            config = replace(
                config,
                production_eval_min_cases=100,
                production_eval_min_positive_cases=80,
                production_eval_min_refusal_cases=20,
            )
            report["attestation"]["dataset_sha256"] = "stale"
            Path(config.eval_test_report_path).write_text(json.dumps(report), encoding="utf-8")
            failures = readiness(config, database)["failures"]
            self.assertTrue(any("production minimum" in item for item in failures))
            self.assertIn("frozen test dataset fingerprint is stale", failures)
