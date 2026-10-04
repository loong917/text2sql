"""Versioned knowledge-artifact registry with atomic active-pointer updates."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .atomic import atomic_json


@dataclass(frozen=True)
class KnowledgeArtifact:
    version: str
    collection_name: str
    knowledge_index_path: str
    calibrator_path: str
    manifest_path: str
    report_path: str
    schema_fingerprint: str
    created_at: str

    @property
    def snapshot_path(self) -> str:
        return str(Path(self.knowledge_index_path).parent / "knowledge_snapshot.json")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeArtifact:
        return cls(
            version=str(payload["version"]),
            collection_name=str(payload["collection_name"]),
            knowledge_index_path=str(payload["knowledge_index_path"]),
            calibrator_path=str(payload["calibrator_path"]),
            manifest_path=str(payload["manifest_path"]),
            report_path=str(payload["report_path"]),
            schema_fingerprint=str(payload.get("schema_fingerprint") or ""),
            created_at=str(payload.get("created_at") or ""),
        )


@dataclass
class TrainingLease:
    """Ownership token for one cross-process training run."""

    path: Path
    token: str

    def release(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if payload.get("token") == self.token:
            self.path.unlink(missing_ok=True)


class KnowledgeArtifactRegistry:
    def __init__(self, root: str | Path, active_pointer: str | Path):
        self.root = Path(root)
        self.active_pointer = Path(active_pointer)

    @property
    def candidate_pointer(self) -> Path:
        return self.root / "candidate_artifact.json"

    def load_version(self, version: str) -> KnowledgeArtifact | None:
        """Resolve only a registry-owned version, never a caller-supplied file path."""
        if re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", version) is None:
            return None
        try:
            artifact = KnowledgeArtifact.from_dict(
                json.loads((self.root / version / "artifact.json").read_bytes())
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if artifact.version != version or self.validation_errors(artifact):
            return None
        return artifact

    def load_candidate(self) -> KnowledgeArtifact | None:
        try:
            candidate = KnowledgeArtifact.from_dict(json.loads(self.candidate_pointer.read_bytes()))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        registered = self.load_version(candidate.version)
        return candidate if registered == candidate else None

    def stage(self, artifact: KnowledgeArtifact) -> None:
        """Register a Dev-accepted candidate without touching the serving pointer."""
        errors = self.validation_errors(artifact)
        if errors:
            raise ValueError("incomplete candidate: " + "; ".join(errors))
        report = json.loads(Path(artifact.report_path).read_bytes())
        if report.get("quality_gate", {}).get("passed") is not True:
            raise ValueError("candidate did not pass the Dev quality gate")
        atomic_json(Path(artifact.knowledge_index_path).parent / "artifact.json", asdict(artifact))
        atomic_json(self.candidate_pointer, asdict(artifact))

    @staticmethod
    def release_path(artifact: KnowledgeArtifact) -> Path:
        return Path(artifact.knowledge_index_path).parent / "approved_release.json"

    @staticmethod
    def test_report_path(artifact: KnowledgeArtifact) -> Path:
        return Path(artifact.knowledge_index_path).parent / "test_report.json"

    @classmethod
    def promotion_pointer_bytes(cls, artifact: KnowledgeArtifact) -> bytes:
        payload = asdict(artifact) | {"release_manifest_path": str(cls.release_path(artifact))}
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def candidate(self, *, schema_fingerprint: str) -> KnowledgeArtifact:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        version = f"{timestamp}-{uuid.uuid4().hex[:8]}"
        directory = self.root / version
        return KnowledgeArtifact(
            version=version,
            collection_name=f"knowledge_{version.lower()}",
            knowledge_index_path=str(directory / "knowledge_index.json"),
            calibrator_path=str(directory / "table_retrieval_calibrator.json"),
            manifest_path=str(directory / "training_manifest.json"),
            report_path=str(directory / "training_report.json"),
            schema_fingerprint=schema_fingerprint,
            created_at=datetime.now(UTC).isoformat(),
        )

    def load_active(self) -> KnowledgeArtifact | None:
        if not self.active_pointer.exists():
            return None
        try:
            payload = json.loads(self.active_pointer.read_text(encoding="utf-8"))
            artifact = KnowledgeArtifact.from_dict(payload)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if self.validation_errors(artifact):
            return None
        return artifact

    def validation_errors(self, artifact: KnowledgeArtifact) -> list[str]:
        """Validate pointer provenance and the minimum complete artifact set."""
        expected_directory = (self.root / artifact.version).resolve()
        expected_paths = {
            "knowledge_index": expected_directory / "knowledge_index.json",
            "calibrator": expected_directory / "table_retrieval_calibrator.json",
            "manifest": expected_directory / "training_manifest.json",
            "report": expected_directory / "training_report.json",
            "snapshot": expected_directory / "knowledge_snapshot.json",
        }
        actual_paths = {
            "knowledge_index": Path(artifact.knowledge_index_path).resolve(),
            "calibrator": Path(artifact.calibrator_path).resolve(),
            "manifest": Path(artifact.manifest_path).resolve(),
            "report": Path(artifact.report_path).resolve(),
            "snapshot": Path(artifact.snapshot_path).resolve(),
        }
        errors: list[str] = []
        if re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", artifact.version) is None:
            errors.append("version_invalid")
        expected_collection = f"knowledge_{artifact.version.lower()}"
        if artifact.collection_name != expected_collection:
            errors.append("collection_name_mismatch")
        if not artifact.schema_fingerprint:
            errors.append("schema_fingerprint_missing")
        for name, expected in expected_paths.items():
            actual = actual_paths[name]
            if actual != expected:
                errors.append(f"{name}_path_outside_artifact")
            elif not actual.is_file():
                errors.append(f"{name}_missing")
        if not errors:
            from .snapshot import ArtifactSnapshot

            try:
                snapshot = ArtifactSnapshot.load(artifact.snapshot_path)
                from .provenance import schema_fingerprint

                if schema_fingerprint(snapshot.schema) != artifact.schema_fingerprint:
                    errors.append("snapshot_schema_mismatch")
            except (OSError, ValueError, TypeError, KeyError):
                errors.append("snapshot_invalid")
        return errors

    def acquire_training_lease(self, *, stale_seconds: int) -> TrainingLease:
        """Fail fast when another process owns the knowledge build lease."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".training.lock"
        token = uuid.uuid4().hex
        payload = {
            "token": token,
            "pid": os.getpid(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            try:
                age_seconds = max(0.0, datetime.now().timestamp() - lock_path.stat().st_mtime)
            except OSError:
                age_seconds = 0.0
            diagnostic = (
                " (stale; verify owner termination before manual removal)"
                if age_seconds >= stale_seconds
                else ""
            )
            raise RuntimeError(
                f"knowledge training is already running: {lock_path}{diagnostic}"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
        except BaseException:
            lock_path.unlink(missing_ok=True)
            raise
        return TrainingLease(lock_path, token)

    def publish(self, artifact: KnowledgeArtifact) -> None:
        """Atomically make a fully validated candidate the active artifact."""
        self.active_pointer.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.active_pointer.with_name(
            f".{self.active_pointer.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_bytes(self.promotion_pointer_bytes(artifact))
            os.replace(temporary, self.active_pointer)
        finally:
            if temporary.exists():
                temporary.unlink()

    def prune(
        self,
        *,
        retain_count: int,
        delete_collection: Callable[[str], None] | None = None,
    ) -> list[str]:
        """Remove old versions and their Chroma collections as one safe unit."""
        if retain_count < 1 or not self.root.exists():
            return []
        root = self.root.resolve()
        active = self.load_active()
        active_version = active.version if active else None
        directories = sorted(
            (path for path in self.root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        keep = {path.name for path in directories[:retain_count]}
        if active_version:
            keep.add(active_version)
        candidate = self.load_candidate()
        if candidate is not None:
            keep.add(candidate.version)
        removed: list[str] = []
        for directory in directories:
            resolved = directory.resolve()
            if directory.name in keep or resolved.parent != root:
                continue
            if delete_collection is not None:
                delete_collection(f"knowledge_{directory.name.lower()}")
            shutil.rmtree(resolved)
            removed.append(directory.name)
        return removed
