"""Load leakage-safe retrieval labels and expand them into table pairs."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope

from ..evaluation.dataset import load_evaluation_cases, load_evaluation_cases_bytes


@dataclass(frozen=True)
class RetrievalExample:
    question: str
    positive_tables: tuple[str, ...]
    source: str


def retrieval_dataset_fingerprint(
    *paths: str | Path, reader: Callable[[str | Path], bytes | None] | None = None
) -> str:
    """Fingerprint every split that influences a published calibrator."""
    digest = hashlib.sha256(b"text2sql-retrieval-dataset-v2\0")
    for value in paths:
        path = Path(value)
        content = reader(path) if reader else path.read_bytes()
        if content is None:
            raise ValueError(f"retrieval dataset is missing: {path}")
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def extract_sql_tables(sql: str) -> tuple[str, ...]:
    """Preserve physical Schema keys without treating CTE aliases as tables."""
    tree = parse_one(sql, read="tsql")
    tables: list[str] = []
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                if source.catalog:
                    raise ValueError("retrieval labels cannot reference another database")
                tables.append(
                    f"{source.db}.{source.name}"
                    if source.db and source.db.lower() != "dbo"
                    else source.name
                )
    return tuple(dict.fromkeys(tables))


def load_retrieval_examples(
    train_set_path: str | Path,
    *,
    expected_split: str = "retrieval_train",
    reader: Callable[[str | Path], bytes | None] | None = None,
) -> list[RetrievalExample]:
    examples: list[RetrievalExample] = []
    if reader is None:
        cases = load_evaluation_cases(train_set_path, expected_split=expected_split)
    else:
        content = reader(train_set_path)
        if content is None:
            raise ValueError(f"retrieval dataset is missing: {train_set_path}")
        cases = load_evaluation_cases_bytes(
            content, source=train_set_path, expected_split=expected_split
        )
    for case in cases:
        sql = str(case.payload.get("baseline_sql") or "").strip()
        if sql:
            examples.append(
                RetrievalExample(case.question, extract_sql_tables(sql), expected_split)
            )
        elif bool(case.payload.get("should_refuse")):
            examples.append(RetrievalExample(case.question, (), f"{expected_split}_refusal"))
    deduped: dict[tuple[str, tuple[str, ...]], RetrievalExample] = {}
    for item in examples:
        deduped[(item.question, item.positive_tables)] = item
    return list(deduped.values())


def serialize_pair_records(
    examples: list[RetrievalExample],
    schema: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for example in examples:
        positives = set(example.positive_tables)
        for table_name in schema:
            records.append(
                {
                    "question": example.question,
                    "table_name": table_name,
                    "label": int(table_name in positives),
                    "source": example.source,
                    "hard_negative": False,
                }
            )
    return records
