"""Enforce release evidence at production startup and before accepting queries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.build_info import release_identity
from ..core.config import Settings
from ..core.exceptions import ConfigurationError
from ..core.production import production_configuration_errors
from ..evaluation.report_contract import EvaluationReport
from ..evaluation.reporting import evaluate_configured_quality_gate, summarize_evaluation
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.collection_evidence import assert_collection_evidence
from ..knowledge.snapshot import ArtifactSnapshot
from .evidence import EvidenceSnapshot


class ProductionReleaseGate:
    def __init__(self, config: Settings):
        self.config = config
        self._pinned_version: str | None = None

    def validate(self) -> None:
        if self.config.app_env != "production":
            return
        errors = production_configuration_errors(self.config)
        if errors:
            raise ConfigurationError("生产配置未通过安全检查: " + "; ".join(errors))
        try:
            inputs = EvidenceSnapshot()
            pointer = inputs.json_object(self.config.knowledge_active_pointer_path)
            registry = KnowledgeArtifactRegistry(
                self.config.knowledge_artifact_dir, self.config.knowledge_active_pointer_path
            )
            active = registry.load_active()
            if pointer is None or active is None:
                raise ValueError("active artifact is incomplete")
            approval_path = pointer.get("release_manifest_path")
            if (
                not isinstance(approval_path, str)
                or Path(approval_path).resolve() != registry.release_path(active).resolve()
            ):
                raise ValueError("active pointer has no version-owned release approval")
            report = inputs.json_object(approval_path)
            if report is None:
                raise ValueError("release manifest is missing or invalid")
            self._validate_evidence(report, inputs)
        except Exception as exc:
            raise ConfigurationError(f"生产发布证据失效，禁止服务: {exc}") from exc

    def _validate_evidence(self, release: dict[str, Any], inputs: EvidenceSnapshot) -> None:
        pinned_identity = release_identity(self.config)
        if release.get("status") != "ready" or release.get("failures") != []:
            raise ValueError("release manifest is not ready")
        database = release.get("database") or {}
        if any(
            database.get(key) is not True
            for key in ("success", "connection_encrypted", "read_only_principal")
        ):
            raise ValueError("release has no TLS/read-only database evidence")
        if release.get("release_identity") != pinned_identity:
            raise ValueError("code, model, dependency or access-policy identity changed")
        registry = KnowledgeArtifactRegistry(
            self.config.knowledge_artifact_dir, self.config.knowledge_active_pointer_path
        )
        inputs.read(self.config.knowledge_active_pointer_path)
        active = registry.load_active()
        if active is None:
            raise ValueError("active artifact is incomplete")
        if (release.get("artifact") or {}).get("version") != active.version:
            raise ValueError("release artifact version changed")
        if self._pinned_version is not None and self._pinned_version != active.version:
            raise ValueError("artifact switched; restart with the newly evaluated release")
        evidence = release["evidence"]
        if evidence["active_pointer_sha256"] != inputs.sha256(
            self.config.knowledge_active_pointer_path
        ):
            raise ValueError("active pointer changed")
        paths = {
            "knowledge_index_sha256": active.knowledge_index_path,
            "calibrator_sha256": active.calibrator_path,
            "training_report_sha256": active.report_path,
            "knowledge_snapshot_sha256": active.snapshot_path,
            "manifest_sha256": active.manifest_path,
        }
        if any(
            evidence["artifact_sha256"].get(key) != inputs.sha256(path)
            for key, path in paths.items()
        ):
            raise ValueError("artifact content changed")
        snapshot_bytes = inputs.read(active.snapshot_path)
        if snapshot_bytes is None:
            raise ValueError("missing knowledge snapshot")
        ArtifactSnapshot.from_bytes(snapshot_bytes, require_reviewed=True)
        manifest = inputs.json_object(active.manifest_path) or {}
        index_bytes = inputs.read(active.knowledge_index_path)
        if index_bytes is None:
            raise ValueError("missing knowledge index")
        runtime = RuntimeResources(self.config)
        try:
            assert_collection_evidence(
                runtime.collection_evidence(
                    active.collection_name, index_records=json.loads(index_bytes)
                ),
                (manifest.get("outputs") or {}).get("collection_evidence"),
            )
        finally:
            runtime.close()
        test_path = release.get("test_report_path")
        if (
            not isinstance(test_path, str)
            or Path(test_path).resolve() != registry.test_report_path(active).resolve()
        ):
            raise ValueError("release has no version-owned Test report")
        if evidence["test_report_sha256"] != inputs.sha256(test_path):
            raise ValueError("frozen report changed")
        report_bytes = inputs.read(test_path)
        if report_bytes is None:
            raise ValueError("frozen report is missing")
        test = EvaluationReport.model_validate_json(report_bytes)
        if test.split != "test" or test.attestation.artifact_version != active.version:
            raise ValueError("test did not evaluate this artifact")
        if test.attestation.dataset_sha256 != evidence["test_dataset_sha256"]:
            raise ValueError("test dataset identity changed")
        if test.attestation.artifact_sha256 != evidence["artifact_sha256"]:
            raise ValueError("test did not evaluate these artifact contents")
        identity = test.attestation.release_identity
        if any(identity.get(key) != value for key, value in pinned_identity.items()):
            raise ValueError("test identity changed")
        results = [item.model_dump() for item in test.results]
        if len({item["id"] for item in results}) != len(results):
            raise ValueError("duplicate test case evidence")
        if any(
            item["passed"] is not all(check["passed"] for check in item["checks"])
            for item in results
        ):
            raise ValueError("test verdict disagrees with checks")
        summary = summarize_evaluation(results)
        if (
            summary != test.summary
            or not evaluate_configured_quality_gate(summary, self.config)["passed"]
        ):
            raise ValueError("test quality gate no longer passes")
        for field, minimum in (
            ("total", self.config.production_eval_min_cases),
            ("positive_total", self.config.production_eval_min_positive_cases),
            ("refusal_total", self.config.production_eval_min_refusal_cases),
        ):
            if summary[field] < minimum:
                raise ValueError(f"insufficient frozen {field} evidence")
        if any(item["should_refuse"] and not item["passed"] for item in results):
            raise ValueError("refusal regression")
        if inputs.changed_paths():
            raise ValueError("release evidence changed during validation")
        if release_identity(self.config) != pinned_identity:
            raise ValueError("release identity changed during validation")
        self._pinned_version = active.version
