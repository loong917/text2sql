"""Train and quality-gate the table-retrieval probability calibrator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..core.config import settings
from ..application.context_service import get_live_schema
from ..domain.semantic_ir import parse_question_semantics
from .calibrator import PlattCalibrator
from .dataset import load_retrieval_examples, serialize_pair_records
from .table_card import load_schema_snapshot
from .table_retriever import TableRetriever


async def train_table_retriever() -> dict:
    requested_source = settings.table_retrieval_train_schema_source
    if requested_source not in {"auto", "live", "snapshot"}:
        raise ValueError(
            "TABLE_RETRIEVAL_TRAIN_SCHEMA_SOURCE 必须是 auto、live 或 snapshot"
        )
    schema_source = "live_database"
    schema = {}
    live_error: Exception | None = None
    if requested_source != "snapshot":
        try:
            schema = await get_live_schema(force_refresh=True)
        except Exception as exc:
            live_error = exc
            if requested_source == "live":
                raise
    if not schema:
        schema = load_schema_snapshot(settings.knowledge_index_path)
        schema_source = "knowledge_index_snapshot"
        if not schema:
            raise RuntimeError(
                "实时 Schema 不可用，且 knowledge_index 中没有离线结构快照"
            ) from live_error
    examples = load_retrieval_examples(settings.retrieval_train_set_path)
    if not examples:
        raise RuntimeError("没有可用于训练召回器的 baseline 或 Gold SQL")

    retriever = TableRetriever()
    scores: list[float] = []
    labels: list[int] = []
    score_lookup: dict[tuple[str, str], float] = {}
    for example in examples:
        semantic_ir = parse_question_semantics(example.question)
        for card, score in await retriever.score_all(
            example.question, semantic_ir, schema
        ):
            score_lookup[(example.question, card.table_name)] = score

    records = serialize_pair_records(examples, schema)
    negatives_by_question: dict[str, list[dict]] = {}
    for record in records:
        raw_score = score_lookup[(record["question"], record["table_name"])]
        record["raw_score"] = raw_score
        scores.append(raw_score)
        labels.append(int(record["label"]))
        if not record["label"]:
            negatives_by_question.setdefault(record["question"], []).append(record)
    for negatives in negatives_by_question.values():
        negatives.sort(key=lambda item: -float(item["raw_score"]))
        for record in negatives[:3]:
            record["hard_negative"] = True

    calibrator = PlattCalibrator.fit(scores, labels, target_recall=0.99)
    artifact_path = Path(settings.table_retrieval_calibrator_path)

    dataset_path = artifact_path.with_name("table_retrieval_dataset.jsonl")
    dataset_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    probabilities = [calibrator.predict(score) for score in scores]
    true_positives = sum(
        1
        for probability, label in zip(probabilities, labels)
        if label and probability >= calibrator.threshold
    )
    positives = sum(labels)
    selected_pairs = sum(
        1 for probability in probabilities if probability >= calibrator.threshold
    )
    false_positives = sum(
        1
        for probability, label in zip(probabilities, labels)
        if not label and probability >= calibrator.threshold
    )
    negatives = len(labels) - positives
    false_positive_rate = false_positives / negatives if negatives else 0.0
    accepted = (
        false_positive_rate <= settings.table_retrieval_max_false_positive_rate
    )
    if accepted:
        calibrator.save(artifact_path)
    else:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": "false_positive_rate_exceeded",
                    "false_positive_rate": false_positive_rate,
                    "maximum": settings.table_retrieval_max_false_positive_rate,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    report = {
        "examples": len(examples),
        "schema_source": schema_source,
        "pairs": len(records),
        "positive_pairs": sum(labels),
        "negative_pairs": len(labels) - sum(labels),
        "threshold": calibrator.threshold,
        "target_recall": calibrator.target_recall,
        "artifact_path": str(artifact_path),
        "dataset_path": str(dataset_path),
        "training_table_recall": true_positives / positives if positives else 0.0,
        "negative_false_positive_rate": false_positive_rate,
        "calibrator_accepted": accepted,
        "average_selected_tables": selected_pairs / len(examples),
    }
    report_path = artifact_path.with_name("table_retrieval_report.json")
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    print(json.dumps(asyncio.run(train_table_retriever()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
