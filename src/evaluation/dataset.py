"""Load approved evaluation cases and enforce split integrity."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


VALID_SPLITS = {"retrieval_train", "dev", "test"}


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    split: str
    question: str
    payload: dict[str, Any]


def _read_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [
            json.loads(line)
            for line in text.splitlines()
            if line.strip()
        ]
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
        question = str(payload.get("question") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not case_id or not question:
            raise ValueError(f"{source}:{line_number}: id and question are required")
        if split not in VALID_SPLITS:
            raise ValueError(f"{source}:{line_number}: invalid split '{split}'")
        if expected_split and split != expected_split:
            raise ValueError(
                f"{source}:{line_number}: expected split '{expected_split}', got '{split}'"
            )
        if status != "approved":
            raise ValueError(f"{source}:{line_number}: only approved cases are allowed")
        should_refuse = bool(payload.get("should_refuse"))
        if not should_refuse and not str(payload.get("baseline_sql") or "").strip():
            raise ValueError(
                f"{source}:{line_number}: baseline_sql is required for positive cases"
            )
        if split in {"dev", "test"} and not should_refuse:
            if not isinstance(payload.get("expected_semantic_ir"), dict):
                raise ValueError(
                    f"{source}:{line_number}: expected_semantic_ir is required"
                )
        normalized_question = " ".join(question.lower().split())
        if case_id in seen_ids:
            raise ValueError(f"{source}:{line_number}: duplicate id '{case_id}'")
        if normalized_question in seen_questions:
            raise ValueError(f"{source}:{line_number}: duplicate question")
        seen_ids.add(case_id)
        seen_questions.add(normalized_question)
        cases.append(EvaluationCase(case_id, split, question, payload))
    return cases


def assert_disjoint_splits(*groups: list[EvaluationCase]) -> None:
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    for cases in groups:
        for case in cases:
            normalized = " ".join(case.question.lower().split())
            if case.id in seen_ids or normalized in seen_questions:
                raise ValueError(f"evaluation split leakage detected: {case.id}")
            seen_ids.add(case.id)
            seen_questions.add(normalized)
