"""Evaluation diagnostics and release-quality gates."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _group_metrics(results: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        groups[str(item.get(field) or "unknown")].append(item)
    return {
        name: {
            "total": len(items),
            "passed": sum(bool(item.get("passed")) for item in items),
            "pass_rate": round(sum(bool(item.get("passed")) for item in items) / len(items), 4),
        }
        for name, items in sorted(groups.items())
    }


def summarize_evaluation(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build stable aggregate and diagnostic metrics from case results."""
    total = len(results)
    passed = sum(bool(item.get("passed")) for item in results)
    refusal = [item for item in results if bool(item.get("should_refuse"))]
    positive = [item for item in results if not bool(item.get("should_refuse"))]
    failed_checks: Counter[str] = Counter()
    for item in results:
        for check in item.get("checks", []) or []:
            if not bool(check.get("passed")):
                failed_checks[str(check.get("name") or "unknown")] += 1

    check_groups: dict[str, list[bool]] = defaultdict(list)
    for item in results:
        for check in item.get("checks", []) or []:
            name = str(check.get("name") or "unknown")
            family = "semantic_ir" if name.startswith("semantic_ir:") else name.split(":", 1)[0]
            check_groups[family].append(bool(check.get("passed")))
    by_check = {
        name: {
            "total": len(values),
            "passed": sum(values),
            "pass_rate": round(sum(values) / len(values), 4),
        }
        for name, values in sorted(check_groups.items())
    }

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "refusal_total": len(refusal),
        "refusal_passed": sum(bool(item.get("passed")) for item in refusal),
        "refusal_pass_rate": round(
            sum(bool(item.get("passed")) for item in refusal) / len(refusal), 4
        )
        if refusal
        else 1.0,
        "positive_total": len(positive),
        "positive_passed": sum(bool(item.get("passed")) for item in positive),
        "positive_pass_rate": round(
            sum(bool(item.get("passed")) for item in positive) / len(positive), 4
        )
        if positive
        else 1.0,
        "by_category": _group_metrics(results, "category"),
        "by_difficulty": _group_metrics(results, "difficulty"),
        "failed_checks": dict(failed_checks.most_common()),
        "by_check": by_check,
    }


def evaluate_quality_gate(
    summary: dict[str, Any],
    *,
    min_pass_rate: float,
    min_refusal_pass_rate: float,
    min_positive_pass_rate: float = 0.0,
    min_cases: int = 0,
    min_positive_cases: int = 0,
    min_refusal_cases: int = 0,
    min_semantic_ir_pass_rate: float = 0.0,
    min_execution_pass_rate: float = 0.0,
    min_retrieval_recall: float = 0.0,
) -> dict[str, Any]:
    """Evaluate release thresholds without mutating evaluation results."""
    failures: list[str] = []
    pass_rate = float(summary.get("pass_rate") or 0.0)
    refusal_pass_rate = float(summary.get("refusal_pass_rate") or 0.0)
    positive_pass_rate = float(summary.get("positive_pass_rate") or 0.0)
    total = int(summary.get("total") or 0)
    positive_total = int(summary.get("positive_total") or 0)
    refusal_total = int(summary.get("refusal_total") or 0)
    check_metrics = summary.get("by_check") or {}
    if pass_rate < min_pass_rate:
        failures.append(f"pass_rate {pass_rate:.4f} < {min_pass_rate:.4f}")
    if refusal_pass_rate < min_refusal_pass_rate:
        failures.append(f"refusal_pass_rate {refusal_pass_rate:.4f} < {min_refusal_pass_rate:.4f}")
    if positive_pass_rate < min_positive_pass_rate:
        failures.append(
            f"positive_pass_rate {positive_pass_rate:.4f} < {min_positive_pass_rate:.4f}"
        )
    for name, actual, minimum in (
        ("total", total, min_cases),
        ("positive_total", positive_total, min_positive_cases),
        ("refusal_total", refusal_total, min_refusal_cases),
    ):
        if actual < minimum:
            failures.append(f"{name} {actual} < {minimum}")
    for check_name, check_minimum in (
        ("semantic_ir", min_semantic_ir_pass_rate),
        ("execution_success", min_execution_pass_rate),
        ("retrieval_recall", min_retrieval_recall),
    ):
        check_rate = float((check_metrics.get(check_name) or {}).get("pass_rate") or 0.0)
        if check_rate < check_minimum:
            failures.append(f"{check_name}_pass_rate {check_rate:.4f} < {check_minimum:.4f}")
    return {
        "passed": not failures,
        "failures": failures,
        "thresholds": {
            "min_pass_rate": min_pass_rate,
            "min_refusal_pass_rate": min_refusal_pass_rate,
            "min_positive_pass_rate": min_positive_pass_rate,
            "min_cases": min_cases,
            "min_positive_cases": min_positive_cases,
            "min_refusal_cases": min_refusal_cases,
            "min_semantic_ir_pass_rate": min_semantic_ir_pass_rate,
            "min_execution_pass_rate": min_execution_pass_rate,
            "min_retrieval_recall": min_retrieval_recall,
        },
    }
