"""Train and quality-gate the table-retrieval probability calibrator."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..application.context_state import ContextRuntimeState
from ..application.schema_repository import get_live_schema
from ..core.config import Settings
from ..domain.semantic_ir import parse_question_semantics
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.provenance import schema_fingerprint
from ..knowledge.structured import load_validated_knowledge_bundle
from .calibrator import PlattCalibrator
from .dataset import (
    load_retrieval_examples,
    retrieval_dataset_fingerprint,
    serialize_pair_records,
)
from .table_card import load_schema_snapshot
from .table_retriever import OllamaEmbedder, TableRetriever


def publish_calibrator_candidate(
    calibrator: PlattCalibrator,
    artifact_path: Path,
    *,
    accepted: bool,
    table_recall: float,
    false_positive_rate: float,
    maximum_false_positive_rate: float,
) -> dict[str, str | bool]:
    """Publish an accepted candidate atomically while retaining last-known-good."""
    candidate_path = artifact_path.with_name(f"{artifact_path.stem}.candidate.json")
    rejected_path = artifact_path.with_name(
        f"{artifact_path.stem}.rejected.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    )
    previous_path = artifact_path.with_name(f"{artifact_path.stem}.previous.json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if accepted:
        calibrator.save(candidate_path)
        if PlattCalibrator.load(artifact_path) is not None:
            shutil.copy2(artifact_path, previous_path)
        os.replace(candidate_path, artifact_path)
    else:
        rejected_path.write_text(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": "held_out_quality_gate_failed",
                    "table_recall": table_recall,
                    "false_positive_rate": false_positive_rate,
                    "maximum": maximum_false_positive_rate,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        rejected_versions = sorted(
            artifact_path.parent.glob(f"{artifact_path.stem}.rejected.*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale_path in rejected_versions[5:]:
            stale_path.unlink(missing_ok=True)
    return {
        "rejected_artifact_path": str(rejected_path) if not accepted else "",
        "previous_artifact_path": str(previous_path),
        "active_artifact_retained": bool(not accepted and artifact_path.exists()),
    }


async def train_table_retriever(config: Settings, runtime: RuntimeResources) -> dict:
    context_state = ContextRuntimeState()
    requested_source = config.table_retrieval_train_schema_source
    if requested_source not in {"auto", "live", "snapshot"}:
        raise ValueError("TABLE_RETRIEVAL_TRAIN_SCHEMA_SOURCE 必须是 auto、live 或 snapshot")
    schema_source = "live_database"
    schema = {}
    live_error: Exception | None = None
    if requested_source != "snapshot":
        try:
            schema = await get_live_schema(
                config.schema_cache_ttl_seconds,
                force_refresh=True,
                sql_executor=VannaSqlExecutor(
                    runtime.sql_runner, max_concurrency=config.sql_max_concurrency
                ),
                state=context_state,
            )
        except Exception as exc:
            live_error = exc
            if requested_source == "live":
                raise
    if not schema:
        schema = load_schema_snapshot(config.schema_snapshot_path)
        schema_source = "versioned_schema_snapshot"
        if not schema:
            raise RuntimeError(
                "实时 Schema 不可用，且 SCHEMA_SNAPSHOT_PATH 没有可用的离线结构快照"
            ) from live_error
    semantic_catalog = load_validated_knowledge_bundle(
        config.structured_knowledge_dir, schema
    ).semantic_catalog()
    train_examples = load_retrieval_examples(config.retrieval_train_set_path)
    calibration_examples = load_retrieval_examples(
        config.retrieval_calibration_set_path,
        expected_split="retrieval_calibration",
    )
    test_examples = load_retrieval_examples(
        config.retrieval_test_set_path,
        expected_split="retrieval_test",
    )
    if not train_examples or not calibration_examples or not test_examples:
        raise RuntimeError("没有可用于训练召回器的 baseline 或 Gold SQL")

    retriever = TableRetriever(
        embedder=OllamaEmbedder(
            host=config.llm_host,
            timeout_seconds=config.llm_timeout_seconds,
            model=config.embedding_model,
            keep_alive=config.llm_keep_alive,
        ),
        calibrator_path=config.table_retrieval_calibrator_path,
        token_budget=config.table_retrieval_token_budget,
        embedding_model=config.embedding_model,
        require_calibration=config.table_retrieval_require_calibration,
    )

    async def score_examples(examples):
        lookup: dict[tuple[str, str], float] = {}
        semantic_grounding: dict[str, bool] = {}
        for example in examples:
            semantic_ir = parse_question_semantics(example.question, semantic_catalog)
            semantic_grounding[example.question] = semantic_ir.is_grounded
            for card, score in await retriever.score_all(example.question, semantic_ir, schema):
                lookup[(example.question, card.table_name)] = score
        records = serialize_pair_records(examples, schema)
        for record in records:
            record["raw_score"] = lookup[(record["question"], record["table_name"])]
            record["semantic_grounded"] = semantic_grounding[record["question"]]
        return records

    records = await score_examples(train_examples)
    calibration_records = await score_examples(calibration_examples)
    test_records = await score_examples(test_examples)
    scores = [float(item["raw_score"]) for item in records]
    labels = [int(item["label"]) for item in records]
    negatives_by_question: dict[str, list[dict]] = {}
    for record in records:
        if not record["label"]:
            negatives_by_question.setdefault(record["question"], []).append(record)
    for negatives in negatives_by_question.values():
        negatives.sort(key=lambda item: -float(item["raw_score"]))
        for record in negatives[:3]:
            record["hard_negative"] = True

    calibrator = PlattCalibrator.fit(scores, labels, target_recall=0.99)
    calibrator = calibrator.calibrate_threshold(
        [float(item["raw_score"]) for item in calibration_records],
        [int(item["label"]) for item in calibration_records],
    )
    dataset_digest = retrieval_dataset_fingerprint(
        config.retrieval_train_set_path,
        config.retrieval_calibration_set_path,
        config.retrieval_test_set_path,
    )
    calibrator = calibrator.with_provenance(
        schema_fingerprint=schema_fingerprint(schema),
        embedding_model=config.embedding_model,
        dataset_fingerprint=dataset_digest,
    )
    artifact_path = Path(config.table_retrieval_calibrator_path)

    dataset_path = artifact_path.with_name("table_retrieval_dataset.jsonl")
    dataset_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    test_scores = [float(item["raw_score"]) for item in test_records]
    test_labels = [int(item["label"]) for item in test_records]
    probabilities = [calibrator.predict(score) for score in test_scores]
    true_positives = sum(
        1
        for probability, label in zip(probabilities, test_labels, strict=True)
        if label and probability >= calibrator.threshold
    )
    positives = sum(test_labels)
    selected_pairs = sum(
        1
        for probability, record in zip(probabilities, test_records, strict=True)
        if record["semantic_grounded"] and probability >= calibrator.threshold
    )
    false_positives = sum(
        1
        for probability, label, record in zip(probabilities, test_labels, test_records, strict=True)
        if not label and record["semantic_grounded"] and probability >= calibrator.threshold
    )
    negative_count = len(test_labels) - positives
    false_positive_rate = false_positives / negative_count if negative_count else 0.0
    test_recall = true_positives / positives if positives else 0.0
    accepted = (
        test_recall >= calibrator.target_recall
        and false_positive_rate <= config.table_retrieval_max_false_positive_rate
    )
    publication = publish_calibrator_candidate(
        calibrator,
        artifact_path,
        accepted=accepted,
        table_recall=test_recall,
        false_positive_rate=false_positive_rate,
        maximum_false_positive_rate=config.table_retrieval_max_false_positive_rate,
    )
    report = {
        "train_examples": len(train_examples),
        "calibration_examples": len(calibration_examples),
        "test_examples": len(test_examples),
        "schema_source": schema_source,
        "pairs": len(records),
        "positive_pairs": sum(labels),
        "negative_pairs": len(labels) - sum(labels),
        "threshold": calibrator.threshold,
        "target_recall": calibrator.target_recall,
        "artifact_path": str(artifact_path),
        **publication,
        "dataset_path": str(dataset_path),
        "held_out_table_recall": test_recall,
        "negative_false_positive_rate": false_positive_rate,
        "calibrator_accepted": accepted,
        "average_selected_tables": selected_pairs / len(test_examples),
        "semantic_abstained_questions": len(
            {str(record["question"]) for record in test_records if not record["semantic_grounded"]}
        ),
    }
    report_path = artifact_path.with_name("table_retrieval_report.json")
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    from ..core.config import load_settings

    config = load_settings()
    runtime = RuntimeResources(config)
    try:
        result = asyncio.run(train_table_retriever(config, runtime))
    finally:
        runtime.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
