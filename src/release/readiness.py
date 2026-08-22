"""Build a machine-readable, fail-closed production release decision."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.build_info import file_sha256, release_identity
from ..core.config import Settings
from ..core.production import production_configuration_errors
from ..infrastructure.database_preflight import (
    DatabasePreflightReport,
    database_configuration_errors,
    run_database_preflight,
)
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..retrieval.calibrator import PlattCalibrator
from ..retrieval.dataset import retrieval_dataset_fingerprint


def _read_json(path: str | Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _jsonl_counts(path: str | Path) -> tuple[int, int, int] | None:
    try:
        records = [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not all(isinstance(item, dict) for item in records):
        return None
    refusal = sum(bool(item.get("should_refuse")) for item in records)
    return len(records), len(records) - refusal, refusal


def _atomic_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_production_readiness(
    config: Settings,
    *,
    database: DatabasePreflightReport,
    observed_model_digests: dict[str, str],
    knowledge_collection_ready: bool = True,
    calibrator_ready: bool = True,
) -> dict[str, Any]:
    failures: list[str] = []
    production_config = replace(config, app_env="production")
    if config.app_env != "production":
        failures.append("APP_ENV must be production")
    failures.extend(production_configuration_errors(production_config))
    failures.extend(database_configuration_errors(production_config))
    failures.extend(database.configuration_errors)
    if not database.success:
        failures.append(database.error_code or "database preflight failed")
    if database.connection_encrypted is not True:
        failures.append("database connection encryption was not verified")
    if database.read_only_principal is not True:
        failures.append("database read-only principal was not verified")

    artifact = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    ).load_active()
    if artifact is None:
        failures.append("active knowledge artifact is missing or incomplete")
    else:
        if not knowledge_collection_ready:
            failures.append("active Chroma knowledge collection is missing or unreadable")
        if not calibrator_ready:
            failures.append("active artifact calibrator is missing, stale or invalid")
        manifest = _read_json(artifact.manifest_path) or {}
        artifact_hashes = manifest.get("outputs") or {}
        expected_artifact_hashes = {
            "knowledge_index_sha256": file_sha256(artifact.knowledge_index_path),
            "calibrator_sha256": file_sha256(artifact.calibrator_path),
            "training_report_sha256": file_sha256(artifact.report_path),
        }
        for field, actual_hash in expected_artifact_hashes.items():
            if artifact_hashes.get(field) != actual_hash:
                failures.append(f"active artifact content hash is stale: {field}")
        artifact_identity = (manifest.get("inputs") or {}).get("release_identity") or {}
        expected_identity = release_identity(config)
        for field in (
            "app_revision",
            "llm_model",
            "llm_model_digest",
            "embedding_model",
            "embedding_model_digest",
            "prompt_fingerprint",
            "dependency_versions",
            "dependency_lock_fingerprints",
        ):
            if artifact_identity.get(field) != expected_identity.get(field):
                failures.append(f"active artifact release identity is stale: {field}")

    dataset_counts = _jsonl_counts(config.eval_test_set_path)
    if dataset_counts is None:
        failures.append("frozen test dataset is missing or invalid")
    else:
        total, positive, refusal = dataset_counts
        for label, actual, minimum in (
            ("total", total, config.production_eval_min_cases),
            ("positive", positive, config.production_eval_min_positive_cases),
            ("refusal", refusal, config.production_eval_min_refusal_cases),
        ):
            if actual < minimum:
                failures.append(
                    f"frozen test dataset {label} cases {actual} is below production minimum {minimum}"
                )
    for label, path, minimum in (
        (
            "retrieval calibration",
            config.retrieval_calibration_set_path,
            config.production_retrieval_calibration_min_cases,
        ),
        (
            "retrieval test",
            config.retrieval_test_set_path,
            config.production_retrieval_test_min_cases,
        ),
    ):
        counts = _jsonl_counts(path)
        if counts is None:
            failures.append(f"{label} dataset is missing or invalid")
        elif counts[0] < minimum:
            failures.append(f"{label} cases {counts[0]} is below production minimum {minimum}")

    expected_digests = {
        config.llm_model: config.llm_model_digest,
        config.embedding_model: config.embedding_model_digest,
    }
    for model, expected in expected_digests.items():
        actual_digest = observed_model_digests.get(model) or observed_model_digests.get(
            f"{model}:latest"
        )
        if not actual_digest:
            failures.append(f"Ollama model is missing: {model}")
        elif expected and actual_digest != expected:
            failures.append(f"Ollama model digest mismatch: {model}")

    test_report = _read_json(config.eval_test_report_path)
    if test_report is None:
        failures.append("frozen test report is missing or invalid")
    else:
        summary = test_report.get("summary") or {}
        attestation = test_report.get("attestation") or {}
        if test_report.get("split") != "test":
            failures.append("evaluation report is not the frozen test split")
        if not (test_report.get("quality_gate") or {}).get("passed"):
            failures.append("frozen test quality gate did not pass")
        thresholds = {
            "total": config.production_eval_min_cases,
            "positive_total": config.production_eval_min_positive_cases,
            "refusal_total": config.production_eval_min_refusal_cases,
        }
        for field, minimum in thresholds.items():
            if int(summary.get(field) or 0) < minimum:
                failures.append(f"frozen test {field} is below production minimum {minimum}")
        if attestation.get("dataset_sha256") != file_sha256(config.eval_test_set_path):
            failures.append("frozen test dataset fingerprint is stale")
        if artifact is not None:
            if attestation.get("artifact_version") != artifact.version:
                failures.append("frozen test was not run against the active artifact")
            if attestation.get("schema_fingerprint") != artifact.schema_fingerprint:
                failures.append("frozen test schema fingerprint is stale")
        tested_identity = attestation.get("release_identity") or {}
        expected_identity = release_identity(config)
        for field in (
            "app_revision",
            "llm_model",
            "llm_model_digest",
            "embedding_model",
            "embedding_model_digest",
            "prompt_fingerprint",
            "dependency_versions",
            "dependency_lock_fingerprints",
        ):
            if tested_identity.get(field) != expected_identity.get(field):
                failures.append(f"frozen test release identity is stale: {field}")
        tested_model_digests = tested_identity.get("observed_model_digests") or {}
        for model, expected in expected_digests.items():
            tested_digest = tested_model_digests.get(model) or tested_model_digests.get(
                f"{model}:latest"
            )
            if tested_digest != expected:
                failures.append(f"frozen test observed model digest mismatch: {model}")
        critical_failures = []
        for result in test_report.get("results") or []:
            if result.get("should_refuse") and not result.get("passed"):
                critical_failures.append(str(result.get("id") or "unknown"))
                continue
            for check in result.get("checks") or []:
                name = str(check.get("name") or "")
                if name.startswith(("not_contains", "not_column")) and not check.get("passed"):
                    critical_failures.append(str(result.get("id") or "unknown"))
                    break
        if critical_failures:
            failures.append(
                "security/refusal cases must have zero failures: "
                + ", ".join(sorted(set(critical_failures)))
            )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "ready" if not failures else "blocked",
        "failures": list(dict.fromkeys(failures)),
        "database": database.to_dict(),
        "artifact": {
            "version": artifact.version,
            "schema_fingerprint": artifact.schema_fingerprint,
            "manifest_path": artifact.manifest_path,
        }
        if artifact
        else None,
        "test_report_path": config.eval_test_report_path,
        "release_identity": release_identity(config),
        "observed_model_digests": observed_model_digests,
    }


def main() -> None:
    from ..core.config import load_settings

    config = load_settings()
    production_config = replace(config, app_env="production")
    database = run_database_preflight(production_config)
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    )
    artifact = registry.load_active()
    runtime = RuntimeResources(config)
    try:
        try:
            model_digests = runtime.model_digests()
        except Exception:
            model_digests = {}
        try:
            if artifact is None:
                raise RuntimeError("missing artifact")
            runtime.probe_knowledge_collection(artifact.collection_name)
            knowledge_collection_ready = True
        except Exception:
            knowledge_collection_ready = False
    finally:
        runtime.close()
    calibrator_ready = False
    if artifact is not None:
        calibrator_ready = (
            PlattCalibrator.load(
                Path(artifact.calibrator_path),
                expected_schema_fingerprint=artifact.schema_fingerprint,
                expected_embedding_model=config.embedding_model,
                expected_dataset_fingerprint=retrieval_dataset_fingerprint(
                    config.retrieval_train_set_path,
                    config.retrieval_calibration_set_path,
                    config.retrieval_test_set_path,
                ),
            )
            is not None
        )
    report = build_production_readiness(
        config,
        database=database,
        observed_model_digests=model_digests,
        knowledge_collection_ready=knowledge_collection_ready,
        calibrator_ready=calibrator_ready,
    )
    _atomic_json(config.production_readiness_report_path, report)
    if report["status"] == "ready":
        _atomic_json(config.production_release_manifest_path, report)
    else:
        # Never leave a stale current-release marker after a failed recheck.
        Path(config.production_release_manifest_path).unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "ready":
        raise SystemExit(1)
