"""Pure builders for training reports and version manifests."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..core.config import Settings
from ..knowledge.source_types import STRUCTURE_ONLY_SOURCE_TYPES


def empty_training_report(
    config: Settings,
    include_samples: bool,
    sample_rows: int,
    *,
    started_at: str,
) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "finished_at": None,
        "include_samples": include_samples,
        "sample_rows": sample_rows,
        "skipped": False,
        "skip_reason": None,
        "evaluation_split": config.training_eval_split,
        "table_count": 0,
        "column_count": 0,
        "knowledge_records": 0,
        "memory_records": 0,
        "knowledge_records_by_type": {},
        "memory_records_by_type": {},
        "structure_records": 0,
        "deduped_records_removed": 0,
        "knowledge_source": "structured",
        "structured_files": 0,
        "structured_records": 0,
        "structured_records_rejected": 0,
        "question_sql_examples": 0,
        "question_sql_examples_trained": 0,
        "feedback_examples": 0,
        "feedback_examples_rejected": 0,
        "feedback_examples_loaded": 0,
        "profiling_tables_considered": 0,
        "profiling_columns_selected": 0,
        "evaluations": [],
        "warnings": [],
    }


def finalize_training_report(
    report: dict[str, Any],
    index_records: list[dict[str, Any]],
    *,
    finished_at: str,
) -> dict[str, Any]:
    counts = Counter(record.get("source_type", "unknown") for record in index_records)
    memory_counts = Counter(
        record.get("source_type", "unknown")
        for record in index_records
        if record.get("source_type") not in STRUCTURE_ONLY_SOURCE_TYPES
    )
    report["finished_at"] = finished_at
    report["knowledge_records"] = len(index_records)
    report["knowledge_records_by_type"] = dict(sorted(counts.items()))
    report["memory_records"] = sum(memory_counts.values())
    report["memory_records_by_type"] = dict(sorted(memory_counts.items()))
    report["structure_records"] = report["knowledge_records"] - report["memory_records"]
    return report


def build_training_manifest(
    *,
    generated_at: str,
    include_samples: bool,
    sample_rows: int,
    training_tables: list[str],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    fingerprint: dict[str, Any],
    artifact_hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "include_samples": include_samples,
        "sample_rows": sample_rows,
        "training_tables": training_tables,
        "record_count": len(index_records),
        "record_types": sorted({record.get("source_type", "") for record in index_records}),
        "table_names": sorted(
            {
                table_name
                for record in index_records
                for table_name in record.get("table_names", [])
                if table_name
            }
        ),
        "inputs": fingerprint,
        "outputs": artifact_hashes,
        "summary": {
            "table_count": report.get("table_count", 0),
            "column_count": report.get("column_count", 0),
            "memory_records": report.get("memory_records", 0),
            "structure_records": report.get("structure_records", 0),
            "question_sql_examples": report.get("question_sql_examples", 0),
            "question_sql_examples_trained": report.get("question_sql_examples_trained", 0),
            "feedback_examples": report.get("feedback_examples", 0),
            "deduped_records_removed": report.get("deduped_records_removed", 0),
        },
    }
