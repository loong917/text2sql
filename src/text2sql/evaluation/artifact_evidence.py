"""Content identity of the exact knowledge artifact consumed by an evaluation run."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from ..core.build_info import file_sha256
from ..knowledge.artifacts import KnowledgeArtifact

ARTIFACT_HASH_FIELDS = {
    "knowledge_index_sha256",
    "calibrator_sha256",
    "training_report_sha256",
    "knowledge_snapshot_sha256",
    "manifest_sha256",
}


def artifact_file_hashes(artifact: KnowledgeArtifact) -> dict[str, str]:
    return {
        "knowledge_index_sha256": file_sha256(artifact.knowledge_index_path),
        "calibrator_sha256": file_sha256(artifact.calibrator_path),
        "training_report_sha256": file_sha256(artifact.report_path),
        "knowledge_snapshot_sha256": file_sha256(artifact.snapshot_path),
        "manifest_sha256": file_sha256(artifact.manifest_path),
    }


@dataclass(frozen=True)
class FrozenArtifactFiles:
    artifact: KnowledgeArtifact
    hashes: dict[str, str]


@contextmanager
def freeze_artifact_files(artifact: KnowledgeArtifact) -> Iterator[FrozenArtifactFiles]:
    """Consume and attest the same five file byte streams, despite source ABA changes."""
    sources = {
        "knowledge_index_sha256": (artifact.knowledge_index_path, "knowledge_index.json"),
        "calibrator_sha256": (artifact.calibrator_path, "table_retrieval_calibrator.json"),
        "training_report_sha256": (artifact.report_path, "training_report.json"),
        "knowledge_snapshot_sha256": (artifact.snapshot_path, "knowledge_snapshot.json"),
        "manifest_sha256": (artifact.manifest_path, "training_manifest.json"),
    }
    contents = {name: Path(path).read_bytes() for name, (path, _) in sources.items()}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
    with TemporaryDirectory(prefix="text2sql-evaluation-") as directory:
        root = Path(directory)
        for name, (_, filename) in sources.items():
            (root / filename).write_bytes(contents[name])
        frozen = replace(
            artifact,
            knowledge_index_path=str(root / "knowledge_index.json"),
            calibrator_path=str(root / "table_retrieval_calibrator.json"),
            report_path=str(root / "training_report.json"),
            manifest_path=str(root / "training_manifest.json"),
        )
        yield FrozenArtifactFiles(frozen, hashes)
