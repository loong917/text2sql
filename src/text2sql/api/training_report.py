"""Read and summarize the report belonging to the active artifact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..knowledge.artifacts import KnowledgeArtifactRegistry


def _read_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_training_report_summary(report: dict, manifest: dict | None) -> dict:
    evaluation_summary = report.get("evaluation_summary") or {}
    total = int(evaluation_summary.get("total") or 0)
    passed = int(evaluation_summary.get("passed") or 0)
    failed = int(evaluation_summary.get("failed") or 0)
    pass_rate = round(passed * 100 / total, 1) if total else None
    table_names = []
    baseline_failed_cases = []
    if isinstance(manifest, dict):
        table_names = [str(item) for item in manifest.get("table_names", [])[:8]]
    for item in report.get("evaluations", []) or []:
        checks = item.get("checks", []) or []
        failed_check_names = [
            str(check.get("name") or "") for check in checks if not bool(check.get("passed"))
        ]
        if not (
            item.get("baseline_error")
            or "baseline_execution_success" in failed_check_names
            or "baseline_result_columns" in failed_check_names
            or "baseline_result_row_count" in failed_check_names
            or "baseline_result_match" in failed_check_names
        ):
            continue
        baseline_failed_cases.append(
            {
                "case_index": item.get("case_index"),
                "question": item.get("question"),
                "actual_sql": item.get("actual_sql"),
                "error": item.get("error"),
                "baseline_error": item.get("baseline_error"),
                "result_row_count": item.get("result_row_count", 0),
                "failed_checks": failed_check_names,
            }
        )

    return {
        "finished_at": report.get("finished_at"),
        "include_samples": bool(report.get("include_samples")),
        "sample_rows": report.get("sample_rows"),
        "table_count": report.get("table_count", 0),
        "column_count": report.get("column_count", 0),
        "knowledge_records": report.get("knowledge_records", 0),
        "feedback_examples": report.get("feedback_examples", 0),
        "question_sql_examples": report.get("question_sql_examples", 0),
        "warnings_count": len(report.get("warnings", []) or []),
        "evaluation_total": total,
        "evaluation_passed": passed,
        "evaluation_failed": failed,
        "evaluation_pass_rate": pass_rate,
        "table_names_preview": table_names,
        "baseline_failed_count": len(baseline_failed_cases),
        "baseline_failed_cases": baseline_failed_cases[:5],
    }


def load_active_training_report(config: Settings) -> dict[str, Any]:
    active = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    ).load_active()
    if active is None:
        return {
            "success": True,
            "available": False,
            "error": "尚未发布完整的活动知识版本",
            "summary": None,
            "report": None,
            "manifest": None,
        }
    report = _read_json_file(Path(active.report_path))
    manifest = _read_json_file(Path(active.manifest_path))
    manifest_payload = manifest if isinstance(manifest, dict) else None
    if not isinstance(report, dict):
        return {
            "success": True,
            "available": False,
            "error": None,
            "summary": None,
            "report": None,
            "manifest": manifest_payload,
        }
    return {
        "success": True,
        "available": True,
        "error": None,
        "summary": build_training_report_summary(report, manifest_payload),
        "report": report,
        "manifest": manifest_payload,
    }
