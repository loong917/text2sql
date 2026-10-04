"""Build a machine-readable, fail-closed production release decision."""

from __future__ import annotations

import argparse
import hashlib
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
from ..evaluation.gold_set import configured_generation_errors
from ..evaluation.report_contract import validate_evaluation_report
from ..evaluation.reporting import evaluate_configured_quality_gate, summarize_evaluation
from ..infrastructure.database_preflight import (
    DatabasePreflightReport,
    database_configuration_errors,
    run_database_preflight,
)
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.artifacts import KnowledgeArtifact, KnowledgeArtifactRegistry
from ..knowledge.collection_evidence import assert_collection_evidence
from ..knowledge.snapshot import ArtifactSnapshot
from ..knowledge.structured import load_validated_knowledge_bundle
from ..retrieval.calibrator import PlattCalibrator
from ..retrieval.dataset import retrieval_dataset_fingerprint
from ..retrieval.table_card import business_cards_fingerprint
from .evidence import EvidenceSnapshot, release_output_errors


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _jsonl_counts(
    inputs: EvidenceSnapshot, path: str | Path, *, expected_split: str
) -> tuple[int, int, int] | None:
    try:
        cases = inputs.evaluation_cases(path, split=expected_split)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not cases:
        return None
    refusal = sum(case.payload["should_refuse"] for case in cases)
    return len(cases), len(cases) - refusal, refusal


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
    knowledge_collection_ready: bool = False,
    calibrator_ready: bool = False,
    selected_artifact: KnowledgeArtifact | None = None,
    observed_collection_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    inputs = EvidenceSnapshot()
    pinned_identity = release_identity(config)
    inputs.read(config.knowledge_active_pointer_path)
    source_paths = {
        "test_dataset": config.eval_test_set_path,
        "retrieval_train": config.retrieval_train_set_path,
        "retrieval_calibration": config.retrieval_calibration_set_path,
        "retrieval_test": config.retrieval_test_set_path,
        "dev_dataset": config.eval_dev_set_path,
        "gold_generation_manifest": str(
            Path(config.eval_test_set_path).parent.parent / "gold-manifest.json"
        ),
        "gold_canonical": str(Path(config.eval_test_set_path).parent.parent / "gold-cases.jsonl"),
    }
    failures.extend(
        configured_generation_errors(
            {
                "test": config.eval_test_set_path,
                "dev": config.eval_dev_set_path,
                "retrieval_train": config.retrieval_train_set_path,
                "retrieval_calibration": config.retrieval_calibration_set_path,
                "retrieval_test": config.retrieval_test_set_path,
            },
            config.structured_knowledge_dir,
            reader=inputs.read,
        )
    )
    source_hashes = {name: inputs.sha256(path) for name, path in source_paths.items()}
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

    expected_artifact_hashes: dict[str, str] = {}
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    )
    artifact = selected_artifact or registry.load_active()
    failures.extend(release_output_errors(config, artifact))
    if artifact is not None and registry.validation_errors(artifact):
        failures.append("selected knowledge artifact is incomplete")
        artifact = None
    if artifact is None:
        failures.append("active knowledge artifact is missing or incomplete")
    else:
        config = replace(config, eval_test_report_path=str(registry.test_report_path(artifact)))
        if not knowledge_collection_ready:
            failures.append("active Chroma knowledge collection is missing or unreadable")
        if not calibrator_ready:
            failures.append("active artifact calibrator is missing, stale or invalid")
        manifest = inputs.json_object(artifact.manifest_path) or {}
        artifact_hashes = _object(manifest.get("outputs"))
        try:
            snapshot_bytes = inputs.read(artifact.snapshot_path)
            if snapshot_bytes is None:
                raise ValueError("missing knowledge snapshot")
            snapshot = ArtifactSnapshot.from_bytes(snapshot_bytes, require_reviewed=True)
            approved = load_validated_knowledge_bundle(
                config.structured_knowledge_dir,
                snapshot.schema,
                require_reviewed=True,
                reader=inputs.read,
            )
            if approved.fingerprint != snapshot.knowledge.fingerprint:
                raise ValueError("artifact knowledge differs from the approved Gold generation")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            failures.append(f"production knowledge approval invalid: {exc}")
        try:
            assert_collection_evidence(
                observed_collection_evidence, artifact_hashes.get("collection_evidence")
            )
        except (ValueError, TypeError, KeyError) as exc:
            failures.append(f"knowledge collection evidence invalid: {exc}")
        expected_artifact_hashes = {
            "knowledge_index_sha256": inputs.sha256(artifact.knowledge_index_path),
            "calibrator_sha256": inputs.sha256(artifact.calibrator_path),
            "training_report_sha256": inputs.sha256(artifact.report_path),
            "knowledge_snapshot_sha256": inputs.sha256(artifact.snapshot_path),
        }
        for field, actual_hash in expected_artifact_hashes.items():
            if artifact_hashes.get(field) != actual_hash:
                failures.append(f"active artifact content hash is stale: {field}")
        artifact_identity = _object(_object(manifest.get("inputs")).get("release_identity"))
        expected_identity = pinned_identity
        for field in (
            "app_revision",
            "llm_model",
            "llm_model_digest",
            "embedding_model",
            "embedding_model_digest",
            "prompt_fingerprint",
            "code_fingerprint",
            "security_fingerprint",
            "dependency_versions",
            "dependency_lock_fingerprints",
        ):
            if artifact_identity.get(field) != expected_identity.get(field):
                failures.append(f"active artifact release identity is stale: {field}")

    dataset_counts = _jsonl_counts(inputs, config.eval_test_set_path, expected_split="test")
    if artifact is not None:
        gold_manifest = (
            inputs.json_object(Path(config.eval_test_set_path).parent.parent / "gold-manifest.json")
            or {}
        )
        if gold_manifest.get("schema_fingerprint") != artifact.schema_fingerprint:
            failures.append("Gold generation and active artifact Schema fingerprints disagree")
        for split, path in (
            ("dev", config.eval_dev_set_path),
            ("test", config.eval_test_set_path),
            ("retrieval_train", config.retrieval_train_set_path),
            ("retrieval_calibration", config.retrieval_calibration_set_path),
            ("retrieval_test", config.retrieval_test_set_path),
        ):
            try:
                cases = inputs.evaluation_cases(path, split=split)
                if any(
                    case.payload["review"]["schema_fingerprint"] != artifact.schema_fingerprint
                    for case in cases
                ):
                    failures.append(f"{split}: review evidence belongs to another Schema")
            except (ValueError, OSError):
                failures.append(f"{split}: approved dataset contract is invalid")
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
        counts = _jsonl_counts(inputs, path, expected_split=label.replace(" ", "_"))
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

    test_report = inputs.json_object(config.eval_test_report_path)
    if test_report is None:
        failures.append("frozen test report is missing or invalid")
    else:
        try:
            cases = inputs.evaluation_cases(config.eval_test_set_path, split="test")
        except (OSError, ValueError):
            cases = []
        results, report_errors = validate_evaluation_report(
            test_report, cases, expected_split="test"
        )
        failures.extend(report_errors)
        summary = summarize_evaluation(results)
        gate = evaluate_configured_quality_gate(summary, config)
        if test_report.get("summary") != summary:
            failures.append("frozen test summary contradicts per-case evidence")
        if test_report.get("quality_gate") != gate:
            failures.append("frozen test reported gate differs from current recomputed thresholds")
        attestation = _object(test_report.get("attestation"))
        if test_report.get("split") != "test":
            failures.append("evaluation report is not the frozen test split")
        if not gate["passed"]:
            failures.append("frozen test quality gate did not pass")
            failures.extend(f"frozen test gate: {failure}" for failure in gate["failures"])
        thresholds = {
            "total": config.production_eval_min_cases,
            "positive_total": config.production_eval_min_positive_cases,
            "refusal_total": config.production_eval_min_refusal_cases,
        }
        for field, minimum in thresholds.items():
            if int(summary.get(field) or 0) < minimum:
                failures.append(f"frozen test {field} is below production minimum {minimum}")
        if attestation.get("dataset_sha256") != inputs.sha256(config.eval_test_set_path):
            failures.append("frozen test dataset fingerprint is stale")
        if artifact is not None:
            if attestation.get("artifact_version") != artifact.version:
                failures.append("frozen test was not run against the active artifact")
            if attestation.get("schema_fingerprint") != artifact.schema_fingerprint:
                failures.append("frozen test schema fingerprint is stale")
            full_hashes = expected_artifact_hashes | {
                "manifest_sha256": inputs.sha256(artifact.manifest_path)
            }
            if attestation.get("artifact_sha256") != full_hashes:
                failures.append("frozen test artifact content fingerprints are stale")
        tested_identity = _object(attestation.get("release_identity"))
        expected_identity = pinned_identity
        for field in (
            "app_revision",
            "llm_model",
            "llm_model_digest",
            "embedding_model",
            "embedding_model_digest",
            "prompt_fingerprint",
            "code_fingerprint",
            "security_fingerprint",
            "dependency_versions",
            "dependency_lock_fingerprints",
        ):
            if tested_identity.get(field) != expected_identity.get(field):
                failures.append(f"frozen test release identity is stale: {field}")
        tested_model_digests = _object(tested_identity.get("observed_model_digests"))
        for model, expected in expected_digests.items():
            tested_digest = tested_model_digests.get(model) or tested_model_digests.get(
                f"{model}:latest"
            )
            if tested_digest != expected:
                failures.append(f"frozen test observed model digest mismatch: {model}")
        critical_failures = []
        for result in results:
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

    changed = inputs.changed_paths()
    if changed:
        failures.append("release inputs changed during validation: " + ", ".join(changed))
    if release_identity(config) != pinned_identity:
        failures.append("release identity changed during validation")
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
        "release_identity": pinned_identity,
        "observed_model_digests": observed_model_digests,
        "evidence": {
            "test_report_sha256": inputs.sha256(config.eval_test_report_path),
            "test_dataset_sha256": inputs.sha256(config.eval_test_set_path),
            "active_pointer_sha256": hashlib.sha256(
                registry.promotion_pointer_bytes(artifact)
            ).hexdigest()
            if artifact
            else "missing",
            "previous_active_pointer_sha256": inputs.sha256(config.knowledge_active_pointer_path),
            "source_sha256": source_hashes,
            "artifact_sha256": expected_artifact_hashes
            | ({"manifest_sha256": inputs.sha256(artifact.manifest_path)} if artifact else {}),
        },
    }


def publication_evidence_errors(
    config: Settings, report: dict[str, Any], *, selected_artifact: KnowledgeArtifact | None = None
) -> list[str]:
    """Recheck pinned bytes immediately before publishing a ready marker."""
    evidence = _object(report.get("evidence"))
    sources = _object(evidence.get("source_sha256"))
    paths = {
        config.knowledge_active_pointer_path: evidence.get("previous_active_pointer_sha256"),
        config.eval_test_set_path: sources.get("test_dataset"),
        config.retrieval_train_set_path: sources.get("retrieval_train"),
        config.retrieval_calibration_set_path: sources.get("retrieval_calibration"),
        config.retrieval_test_set_path: sources.get("retrieval_test"),
        config.eval_dev_set_path: sources.get("dev_dataset"),
        str(Path(config.eval_test_set_path).parent.parent / "gold-manifest.json"): sources.get(
            "gold_generation_manifest"
        ),
        str(Path(config.eval_test_set_path).parent.parent / "gold-cases.jsonl"): sources.get(
            "gold_canonical"
        ),
    }
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir, config.knowledge_active_pointer_path
    )
    active = selected_artifact or registry.load_active()
    if (
        active is None
        or registry.validation_errors(active)
        or active.version != _object(report.get("artifact")).get("version")
    ):
        return ["active artifact changed before release publication"]
    test_path = str(registry.test_report_path(active))
    if Path(str(report.get("test_report_path", ""))).resolve() != Path(test_path).resolve():
        return ["release report is not bound to a version-owned Test report"]
    paths[test_path] = evidence.get("test_report_sha256")
    artifact_hashes = _object(evidence.get("artifact_sha256"))
    paths.update(
        {
            active.knowledge_index_path: artifact_hashes.get("knowledge_index_sha256"),
            active.calibrator_path: artifact_hashes.get("calibrator_sha256"),
            active.report_path: artifact_hashes.get("training_report_sha256"),
            active.snapshot_path: artifact_hashes.get("knowledge_snapshot_sha256"),
            active.manifest_path: artifact_hashes.get("manifest_sha256"),
        }
    )
    errors: list[str] = configured_generation_errors(
        {
            "test": config.eval_test_set_path,
            "dev": config.eval_dev_set_path,
            "retrieval_train": config.retrieval_train_set_path,
            "retrieval_calibration": config.retrieval_calibration_set_path,
            "retrieval_test": config.retrieval_test_set_path,
        },
        config.structured_knowledge_dir,
    )
    for path, expected in paths.items():
        try:
            current = file_sha256(path)
        except FileNotFoundError:
            current = "missing"
        except OSError:
            current = "unreadable"
        if current != expected:
            errors.append(f"release input changed before publication: {path}")
    if release_identity(config) != report.get("release_identity"):
        errors.append("release identity changed before publication")
    return errors


def main() -> None:
    from ..core.config import load_settings

    parser = argparse.ArgumentParser(
        description="Validate a release; promote only with explicit --promote"
    )
    parser.add_argument("--artifact", help="registered candidate version to validate")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="atomically switch ACTIVE after every release check passes",
    )
    args = parser.parse_args()
    if args.promote and not args.artifact:
        parser.error("--promote requires --artifact")
    config = load_settings()
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    )
    artifact = registry.load_version(args.artifact) if args.artifact else registry.load_active()
    if args.artifact and artifact is None:
        parser.error("candidate version is missing or invalid")
    output_errors = release_output_errors(config, artifact)
    if output_errors:
        parser.error("; ".join(output_errors))
    if artifact is not None:
        config = replace(config, eval_test_report_path=str(registry.test_report_path(artifact)))
    production_config = replace(config, app_env="production")
    database = run_database_preflight(production_config)
    runtime = RuntimeResources(config)
    observed_collection = None
    try:
        try:
            model_digests = runtime.model_digests()
        except Exception:
            model_digests = {}
        try:
            if artifact is None:
                raise RuntimeError("missing artifact")
            index = json.loads(Path(artifact.knowledge_index_path).read_bytes())
            expected_collection = (
                json.loads(Path(artifact.manifest_path).read_bytes())
                .get("outputs", {})
                .get("collection_evidence")
            )
            observed_collection = runtime.collection_evidence(
                artifact.collection_name, index_records=index
            )
            assert_collection_evidence(observed_collection, expected_collection)
            knowledge_collection_ready = True
        except Exception:
            knowledge_collection_ready = False
    finally:
        runtime.close()
    calibrator_ready = False
    if artifact is not None:
        try:
            snapshot = ArtifactSnapshot.load(artifact.snapshot_path, require_reviewed=True)
            calibrator_ready = (
                PlattCalibrator.load(
                    Path(artifact.calibrator_path),
                    expected_schema_fingerprint=artifact.schema_fingerprint,
                    expected_embedding_model=config.embedding_model,
                    expected_embedding_model_digest=config.embedding_model_digest,
                    expected_dataset_fingerprint=retrieval_dataset_fingerprint(
                        config.retrieval_train_set_path,
                        config.retrieval_calibration_set_path,
                        config.retrieval_test_set_path,
                    ),
                    expected_business_card_fingerprint=business_cards_fingerprint(
                        snapshot.knowledge.table_cards
                    ),
                )
                is not None
            )
        except (OSError, ValueError, TypeError, KeyError):
            calibrator_ready = False
    report = build_production_readiness(
        config,
        database=database,
        observed_model_digests=model_digests,
        knowledge_collection_ready=knowledge_collection_ready,
        calibrator_ready=calibrator_ready,
        selected_artifact=artifact,
        observed_collection_evidence=observed_collection,
    )
    _atomic_json(config.production_readiness_report_path, report)
    if report["status"] == "ready":
        publication_errors = publication_evidence_errors(config, report, selected_artifact=artifact)
        if publication_errors:
            report["status"] = "blocked"
            report["failures"].extend(publication_errors)
            _atomic_json(config.production_readiness_report_path, report)
    if report["status"] == "ready" and args.promote:
        assert artifact is not None
        promote_release(config, artifact, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "ready":
        raise SystemExit(1)


def promote_release(config: Settings, artifact: KnowledgeArtifact, report: dict[str, Any]) -> None:
    """Persist a version-owned approval, then atomically switch its serving pointer."""
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir, config.knowledge_active_pointer_path
    )
    lease = registry.acquire_training_lease(
        stale_seconds=config.knowledge_training_lock_stale_seconds
    )
    try:
        errors = release_output_errors(config, artifact)
        if errors:
            raise ValueError("unsafe release output: " + "; ".join(errors))
        if registry.release_path(artifact).exists():
            raise ValueError("approved artifacts are immutable; promote a new candidate version")
        if report.get("status") != "ready" or report.get("failures") != []:
            raise ValueError("cannot promote a blocked release")
        errors = publication_evidence_errors(config, report, selected_artifact=artifact)
        if errors:
            raise ValueError("release changed before promotion: " + "; ".join(errors))
        expected = hashlib.sha256(registry.promotion_pointer_bytes(artifact)).hexdigest()
        if report["evidence"]["active_pointer_sha256"] != expected:
            raise ValueError("release is not bound to this promotion pointer")
        archive_path = Path(config.production_release_manifest_path).resolve()
        if (
            archive_path.is_relative_to(registry.root.resolve())
            and archive_path != registry.release_path(artifact).resolve()
        ):
            raise ValueError("release archive cannot overwrite another artifact's approval")
        runtime = RuntimeResources(config)
        try:
            if runtime.model_digests() != report.get("observed_model_digests"):
                raise ValueError("Ollama model identities changed before promotion")
            manifest = json.loads(Path(artifact.manifest_path).read_bytes())
            index = json.loads(Path(artifact.knowledge_index_path).read_bytes())
            assert_collection_evidence(
                runtime.collection_evidence(artifact.collection_name, index_records=index),
                manifest.get("outputs", {}).get("collection_evidence"),
            )
        finally:
            runtime.close()
        # The runtime follows the version-owned approval selected by ACTIVE.
        # A failed candidate never overwrites the previous version's approval.
        _atomic_json(registry.release_path(artifact), report)
        _atomic_json(config.production_release_manifest_path, report)
        errors = publication_evidence_errors(config, report, selected_artifact=artifact)
        if errors:
            raise ValueError("release changed before ACTIVE switch: " + "; ".join(errors))
        registry.publish(artifact)
    finally:
        lease.release()
