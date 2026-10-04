"""Read-once evidence bytes and mutation checks for release publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..core.build_info import file_sha256
from ..core.config import Settings
from ..evaluation.dataset import EvaluationCase, load_evaluation_cases_bytes
from ..knowledge.artifacts import KnowledgeArtifact, KnowledgeArtifactRegistry


def release_output_errors(config: Settings, artifact: KnowledgeArtifact | None) -> list[str]:
    """Keep diagnostic/archive writes away from immutable inputs and serving pointers."""
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir, config.knowledge_active_pointer_path
    )
    protected_files = {
        Path(value).resolve()
        for value in (
            config.knowledge_active_pointer_path,
            registry.candidate_pointer,
            config.eval_test_set_path,
            config.eval_dev_set_path,
            config.retrieval_train_set_path,
            config.retrieval_calibration_set_path,
            config.retrieval_test_set_path,
            config.table_retrieval_calibrator_path,
            config.eval_test_report_path,
            config.schema_snapshot_path,
            config.training_state_path,
            config.feedback_db_path,
            Path(config.eval_test_set_path).parent.parent / "gold-manifest.json",
            Path(config.eval_test_set_path).parent.parent / "gold-cases.jsonl",
        )
        if value
    }
    protected_roots = (
        registry.root.resolve(),
        Path(config.structured_knowledge_dir).resolve(),
        Path(__file__).resolve().parents[1],
    )
    chroma_root = Path(config.knowledge_db_dir).resolve()
    legacy_diagnostics = {
        chroma_root / "production-readiness.json",
        chroma_root / "production-release.json",
    }
    outputs = {
        "readiness": Path(config.production_readiness_report_path).resolve(),
        "archive": Path(config.production_release_manifest_path).resolve(),
    }
    errors: list[str] = []
    if outputs["readiness"] == outputs["archive"]:
        errors.append("readiness and release archive paths must be distinct")
    for name, path in outputs.items():
        owned_approval = (
            artifact is not None
            and name == "archive"
            and path == registry.release_path(artifact).resolve()
        )
        overlaps_chroma = (
            path.is_relative_to(chroma_root)
            and not owned_approval
            and path not in legacy_diagnostics
        )
        if (
            path.suffix.lower() != ".json"
            or overlaps_chroma
            or path in protected_files
            or (not owned_approval and any(path.is_relative_to(root) for root in protected_roots))
        ):
            errors.append(f"{name} output overlaps protected release inputs: {path}")
    return errors


class EvidenceSnapshot:
    def __init__(self) -> None:
        self._contents: dict[str, bytes | None] = {}

    def read(self, path: str | Path) -> bytes | None:
        key = str(Path(path).resolve())
        if key not in self._contents:
            try:
                self._contents[key] = Path(key).read_bytes()
            except OSError:
                self._contents[key] = None
        return self._contents[key]

    def sha256(self, path: str | Path) -> str:
        content = self.read(path)
        return hashlib.sha256(content).hexdigest() if content is not None else "missing"

    def json_object(self, path: str | Path) -> dict[str, Any] | None:
        content = self.read(path)
        try:
            payload = json.loads(content) if content is not None else None
        except (ValueError, UnicodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def evaluation_cases(self, path: str | Path, *, split: str) -> list[EvaluationCase]:
        content = self.read(path)
        return (
            load_evaluation_cases_bytes(content, source=path, expected_split=split)
            if content is not None
            else []
        )

    def changed_paths(self) -> list[str]:
        changed: list[str] = []
        for path, content in self._contents.items():
            expected = hashlib.sha256(content).hexdigest() if content is not None else "missing"
            try:
                current = file_sha256(path)
            except FileNotFoundError:
                current = "missing"
            except OSError:
                current = "unreadable"
            if current != expected:
                changed.append(path)
        return changed
