"""Load leakage-safe retrieval labels and expand them into table pairs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one

from ..evaluation import load_evaluation_cases


@dataclass(frozen=True)
class RetrievalExample:
    question: str
    positive_tables: tuple[str, ...]
    source: str


def retrieval_dataset_fingerprint(*paths: str | Path) -> str:
    """Fingerprint every split that influences a published calibrator."""
    digest = hashlib.sha256()
    for value in paths:
        path = Path(value)
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def extract_sql_tables(sql: str) -> tuple[str, ...]:
    tree = parse_one(sql, read="tsql")
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    return tuple(
        dict.fromkeys(
            table.name for table in tree.find_all(exp.Table) if table.name.lower() not in cte_names
        )
    )


def load_retrieval_examples(
    train_set_path: str | Path,
    *,
    expected_split: str = "retrieval_train",
) -> list[RetrievalExample]:
    examples: list[RetrievalExample] = []
    for case in load_evaluation_cases(train_set_path, expected_split=expected_split):
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
