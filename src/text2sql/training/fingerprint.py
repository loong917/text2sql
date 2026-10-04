"""Stable training input identities and validated skip decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.build_info import release_identity
from ..core.config import Settings
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.provenance import schema_fingerprint
from .storage import now_iso, read_json_file, write_json_file

TRAINING_FINGERPRINT_VERSION = 9


def configured_training_tables(config: Settings) -> set[str] | None:
    if not config.training_tables:
        return None
    return {item.strip() for item in config.training_tables.split(",") if item.strip()}


def hash_file(
    path_str: str | None, reader: Callable[[str | Path], bytes | None] | None = None
) -> str:
    if not path_str:
        return ""
    path = Path(path_str)
    content = reader(path) if reader else path.read_bytes() if path.exists() else None
    if content is None:
        return ""
    return hashlib.sha256(content).hexdigest()


def hash_directory(
    path_str: str,
    reader: Callable[[str | Path], bytes | None] | None = None,
    files: tuple[Path, ...] | None = None,
) -> str:
    root = Path(path_str).resolve()
    if not root.exists():
        return ""
    digest = hashlib.sha256()
    for path in sorted(
        files if files is not None else (item for item in root.rglob("*") if item.is_file())
    ):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        content = reader(path) if reader else path.read_bytes()
        if content is None:
            raise ValueError(f"knowledge input disappeared: {path}")
        digest.update(content)
    return digest.hexdigest()


def hash_live_schema(live_schema: dict[str, dict[str, Any]]) -> str:
    return schema_fingerprint(live_schema)


def gold_feedback_fingerprint(examples: list[dict[str, Any]]) -> str:
    """Hash only governed content that can enter this knowledge build.

    Capture timestamps, Pending reviews and SQLite storage/WAL layout have no
    effect on the compiled Gold memories and are deliberately excluded.
    """
    records = [
        {
            "question": str(item.get("question") or ""),
            "sql": str(item.get("sql") or ""),
            "promotion_evidence": item.get("promotion_evidence") or {},
            "candidate_tables": sorted(item.get("candidate_tables") or []),
        }
        for item in examples
    ]
    normalized = sorted(
        json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records
    )
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def build_training_fingerprint(
    config: Settings,
    *,
    include_samples: bool,
    sample_rows: int,
    live_schema_hash: str,
    gold_feedback: list[dict[str, Any]],
    reader: Callable[[str | Path], bytes | None] | None = None,
    knowledge_files: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    def pinned_hash(path: str) -> str:
        return hash_file(path, reader)

    return {
        "version": TRAINING_FINGERPRINT_VERSION,
        "app_env": config.app_env,
        "include_samples": bool(include_samples),
        "sample_rows": int(sample_rows),
        "structured_knowledge_dir": config.structured_knowledge_dir,
        "structured_knowledge_hash": hash_directory(
            config.structured_knowledge_dir, reader, knowledge_files
        ),
        "live_schema_hash": live_schema_hash,
        "retrieval_train_set_hash": pinned_hash(config.retrieval_train_set_path),
        "retrieval_calibration_set_hash": pinned_hash(config.retrieval_calibration_set_path),
        "retrieval_test_set_hash": pinned_hash(config.retrieval_test_set_path),
        "eval_dev_set_hash": pinned_hash(config.eval_dev_set_path),
        "eval_test_set_hash": pinned_hash(config.eval_test_set_path),
        "gold_generation_manifest_hash": pinned_hash(
            str(Path(config.eval_test_set_path).parent.parent / "gold-manifest.json")
        ),
        "training_eval_split": config.training_eval_split,
        "feedback_gold_hash": gold_feedback_fingerprint(gold_feedback),
        "table_retrieval_calibrator_hash": pinned_hash(config.table_retrieval_calibrator_path),
        "training_tables": sorted(configured_training_tables(config) or []),
        "sample_tables": sorted(
            item.strip() for item in (config.sample_tables or "").split(",") if item.strip()
        ),
        "profiling_max_tables": config.profiling_max_tables,
        "profiling_max_columns_per_table": config.profiling_max_columns_per_table,
        "profiling_max_distinct_values": config.profiling_max_distinct_values,
        "profiling_allowed_columns": config.profiling_allowed_columns,
        "profiling_denied_columns": config.profiling_denied_columns,
        "feedback_min_result_rows": config.feedback_min_result_rows,
        "feedback_require_nonempty_result": config.feedback_require_nonempty_result,
        "feedback_require_execution_success": config.feedback_require_execution_success,
        "release_identity": release_identity(config),
    }


def should_skip_training(config: Settings, fingerprint: dict[str, Any]) -> bool:
    if not config.training_skip_unchanged:
        return False
    state = read_json_file(config.training_state_path)
    if not state:
        return False
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir, config.knowledge_active_pointer_path
    )
    artifact = registry.load_candidate() or registry.load_active()
    if artifact is None or state.get("fingerprint") != fingerprint:
        return False
    from ..knowledge.snapshot import ArtifactSnapshot

    try:
        ArtifactSnapshot.load(
            artifact.snapshot_path, require_reviewed=config.app_env == "production"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return state.get("summary", {}).get("artifact_version") == artifact.version


def write_training_state(
    config: Settings, fingerprint: dict[str, Any], report: dict[str, Any]
) -> None:
    payload = {
        "updated_at": now_iso(),
        "fingerprint": fingerprint,
        "summary": {
            "artifact_version": report.get("artifact_version"),
            "table_count": report.get("table_count", 0),
            "column_count": report.get("column_count", 0),
            "knowledge_records": report.get("knowledge_records", 0),
            "memory_records": report.get("memory_records", 0),
            "evaluation_summary": report.get("evaluation_summary", {}),
        },
    }
    write_json_file(config.training_state_path, payload)
