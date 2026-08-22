"""Load approved evaluation cases and enforce split integrity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

VALID_SPLITS = {
    "retrieval_train",
    "retrieval_calibration",
    "retrieval_test",
    "dev",
    "test",
}


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    split: str
    template_id: str
    template_fingerprint: str | None
    question: str
    payload: dict[str, Any]


def sql_template_fingerprint(sql: str) -> str:
    """Return a value-independent fingerprint for one T-SQL query shape.

    Literal values and presentation-only output aliases are deliberately
    removed.  Consequently, changing a year, city, blood type, or result label
    cannot hide that two evaluation records share the same SQL template.
    """
    try:
        tree = parse_one(sql, read="tsql")
    except ParseError as exc:
        raise ValueError(f"baseline_sql is not valid T-SQL: {exc}") from exc

    def normalize(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal):
            return exp.Literal.string("__value__")
        if isinstance(node, exp.Alias):
            return node.this
        return node

    canonical = tree.transform(normalize).sql(
        dialect="tsql",
        normalize=True,
        pretty=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _read_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError(f"{path}: JSON root must be an array")
    return payload


def load_evaluation_cases(
    path: str | Path,
    *,
    expected_split: str | None = None,
) -> list[EvaluationCase]:
    source = Path(path)
    if not source.exists():
        return []
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    for line_number, payload in enumerate(_read_records(source), 1):
        if not isinstance(payload, dict):
            raise ValueError(f"{source}:{line_number}: record must be an object")
        case_id = str(payload.get("id") or "").strip()
        split = str(payload.get("split") or "").strip()
        template_id = str(payload.get("template_id") or "").strip()
        question = str(payload.get("question") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not case_id or not template_id or not question:
            raise ValueError(f"{source}:{line_number}: id, template_id and question are required")
        if split not in VALID_SPLITS:
            raise ValueError(f"{source}:{line_number}: invalid split '{split}'")
        if expected_split and split != expected_split:
            raise ValueError(
                f"{source}:{line_number}: expected split '{expected_split}', got '{split}'"
            )
        if status != "approved":
            raise ValueError(f"{source}:{line_number}: only approved cases are allowed")
        should_refuse = bool(payload.get("should_refuse"))
        baseline_sql = str(payload.get("baseline_sql") or "").strip()
        if not should_refuse and not baseline_sql:
            raise ValueError(f"{source}:{line_number}: baseline_sql is required for positive cases")
        if split in {"dev", "test"} and not should_refuse:
            if not isinstance(payload.get("expected_semantic_ir"), dict):
                raise ValueError(f"{source}:{line_number}: expected_semantic_ir is required")
        normalized_question = " ".join(question.lower().split())
        if case_id in seen_ids:
            raise ValueError(f"{source}:{line_number}: duplicate id '{case_id}'")
        if normalized_question in seen_questions:
            raise ValueError(f"{source}:{line_number}: duplicate question")
        seen_ids.add(case_id)
        seen_questions.add(normalized_question)
        try:
            template_fingerprint = sql_template_fingerprint(baseline_sql) if baseline_sql else None
        except ValueError as exc:
            raise ValueError(f"{source}:{line_number}: {exc}") from exc
        cases.append(
            EvaluationCase(
                case_id,
                split,
                template_id,
                template_fingerprint,
                question,
                payload,
            )
        )
    return cases


def assert_disjoint_splits(*groups: list[EvaluationCase]) -> None:
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    seen_templates: set[str] = set()
    fingerprint_owners: dict[str, str] = {}
    for cases in groups:
        group_ids: set[str] = set()
        group_questions: set[str] = set()
        group_templates: set[str] = set()
        group_fingerprints: dict[str, str] = {}
        for case in cases:
            normalized = " ".join(case.question.lower().split())
            fingerprint_owner = (
                fingerprint_owners.get(case.template_fingerprint)
                if case.template_fingerprint
                else None
            )
            if (
                case.id in seen_ids
                or normalized in seen_questions
                or case.template_id in seen_templates
            ):
                raise ValueError(f"evaluation split leakage detected: {case.id}")
            if fingerprint_owner:
                raise ValueError(
                    f"evaluation SQL template leakage detected: {fingerprint_owner} <-> {case.id}"
                )
            group_ids.add(case.id)
            group_questions.add(normalized)
            group_templates.add(case.template_id)
            if case.template_fingerprint:
                group_fingerprints.setdefault(case.template_fingerprint, case.id)
        seen_ids.update(group_ids)
        seen_questions.update(group_questions)
        seen_templates.update(group_templates)
        fingerprint_owners.update(group_fingerprints)
