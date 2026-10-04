"""Train and quality-gate the table-retrieval probability calibrator."""

from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..application.context_state import ContextRuntimeState
from ..core.config import Settings
from ..core.production import is_production
from ..domain.semantic_ir import QueryPlan, SemanticCatalog, parse_question_semantics
from ..evaluation.dataset import assert_disjoint_splits, load_evaluation_cases_bytes
from ..evaluation.gold_set import assert_training_isolation, configured_generation_errors
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..infrastructure.schema_repository import get_live_schema
from ..knowledge.atomic import atomic_bytes, atomic_json
from ..knowledge.input_snapshot import InputSnapshot, InputSnapshotError
from ..knowledge.provenance import schema_fingerprint
from ..knowledge.schema_contract import require_schema_contract
from ..knowledge.structured import KnowledgeBundle, load_validated_knowledge_bundle
from .calibrator import PlattCalibrator
from .dataset import (
    RetrievalExample,
    load_retrieval_examples,
    retrieval_dataset_fingerprint,
    serialize_pair_records,
)
from .table_card import build_table_cards, business_cards_fingerprint
from .table_retriever import OllamaEmbedder, TableRetriever, select_candidates


def mine_training_pairs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fit all positives and the nearest training negatives for each question.

    The mining count grows with the available negative population. Calibration
    and Test records never enter this function or influence fitting.
    """
    negatives_by_question: dict[str, list[dict[str, Any]]] = {}
    fitted = []
    for record in records:
        record["fit_sample"] = bool(record["label"])
        if record["label"]:
            fitted.append(record)
        else:
            negatives_by_question.setdefault(record["question"], []).append(record)
    for negatives in negatives_by_question.values():
        negatives.sort(key=lambda item: (-float(item["raw_score"]), str(item["table_name"])))
        for record in negatives[: math.ceil(math.sqrt(len(negatives)))]:
            record["hard_negative"] = True
            record["fit_sample"] = True
            fitted.append(record)
    return fitted


def evaluate_retrieval_policy(
    examples: list[RetrievalExample],
    semantic_irs: dict[str, QueryPlan],
    schema: dict[str, dict[str, Any]],
    scored_by_question: dict[str, list],
    calibrator: PlattCalibrator,
    *,
    token_budget: int,
    require_calibration: bool,
    business_cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Measure complete-question recall using the online candidate policy."""
    cards = build_table_cards(schema, business_cards)
    results: list[dict[str, Any]] = []
    complete_positives = positive_questions = 0
    selected_count = false_positives = negative_pairs = recalled_pairs = positive_pairs = 0
    refused = refusal_questions = 0
    for example in examples:
        selection = select_candidates(
            semantic_irs[example.question],
            schema,
            cards,
            scored_by_question[example.question],
            calibrator,
            token_budget=token_budget,
            require_calibration=require_calibration,
        )
        expected = set(example.positive_tables)
        selected = {candidate.table_name for candidate in selection.candidates}
        selected_count += len(selected)
        false_positives += len(selected - expected)
        negative_pairs += len(set(schema) - expected)
        recalled_pairs += len(selected & expected)
        positive_pairs += len(expected)
        if expected:
            positive_questions += 1
            complete = expected <= selected
            complete_positives += int(complete)
        else:
            refusal_questions += 1
            complete = not selected
            refused += int(complete)
        results.append(
            {
                "question": example.question,
                "required_tables": sorted(expected),
                "selected_tables": sorted(selected),
                "complete_required_tables": complete,
                "diagnostics": selection.diagnostics,
            }
        )
    return {
        "held_out_table_recall": complete_positives / positive_questions
        if positive_questions
        else 0.0,
        "table_pair_recall": recalled_pairs / positive_pairs if positive_pairs else 0.0,
        "negative_false_positive_rate": false_positives / negative_pairs if negative_pairs else 0.0,
        "refusal_pass_rate": refused / refusal_questions if refusal_questions else 1.0,
        "average_selected_tables": selected_count / len(examples) if examples else 0.0,
        "semantic_abstained_questions": sum(not ir.is_grounded for ir in semantic_irs.values()),
        "question_results": results,
    }


def publish_calibrator_candidate(
    calibrator: PlattCalibrator,
    artifact_path: Path,
    *,
    accepted: bool,
    table_recall: float,
    false_positive_rate: float,
    maximum_false_positive_rate: float,
    lease: CalibratorLease | None = None,
    pre_publish: Callable[[], None] | None = None,
) -> dict[str, str | bool]:
    """Publish an accepted candidate atomically while retaining last-known-good."""
    if lease is None:
        with calibrator_lease(artifact_path) as owned:
            return publish_calibrator_candidate(
                calibrator,
                artifact_path,
                accepted=accepted,
                table_recall=table_recall,
                false_positive_rate=false_positive_rate,
                maximum_false_positive_rate=maximum_false_positive_rate,
                lease=owned,
                pre_publish=pre_publish,
            )
    lease.assert_owned(artifact_path)
    candidate_path = artifact_path.with_name(
        f".{artifact_path.name}.candidate.{uuid.uuid4().hex}.tmp"
    )
    rejected_path = artifact_path.with_name(
        f"{artifact_path.stem}.rejected.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.{uuid.uuid4().hex}.json"
    )
    previous_path = artifact_path.with_name(f"{artifact_path.stem}.previous.json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if accepted:
        content = calibrator.to_bytes()
        if PlattCalibrator.from_bytes(content) is None:
            raise ValueError("invalid calibrator candidate; previous artifact retained")
        try:
            try:
                previous = artifact_path.read_bytes()
            except FileNotFoundError:
                previous = None
            with candidate_path.open("xb") as stream:
                stream.write(content)
            if previous is not None and PlattCalibrator.from_bytes(previous) is not None:
                atomic_bytes(previous_path, previous)
            if pre_publish is not None:
                pre_publish()
            lease.assert_owned(artifact_path)
            os.replace(candidate_path, artifact_path)
        finally:
            candidate_path.unlink(missing_ok=True)
    else:
        if pre_publish is not None:
            pre_publish()
        atomic_json(
            rejected_path,
            {
                "status": "rejected",
                "reason": "held_out_quality_gate_failed",
                "table_recall": table_recall,
                "false_positive_rate": false_positive_rate,
                "maximum": maximum_false_positive_rate,
            },
        )
        rejected_versions = sorted(
            artifact_path.parent.glob(f"{artifact_path.stem}.rejected.*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale_path in rejected_versions[5:]:
            if stale_path.resolve().parent == artifact_path.parent.resolve():
                stale_path.unlink(missing_ok=True)
    return {
        "rejected_artifact_path": str(rejected_path) if not accepted else "",
        "previous_artifact_path": str(previous_path),
        "active_artifact_retained": bool(not accepted and artifact_path.exists()),
    }


@dataclass(frozen=True)
class CalibratorLease:
    path: Path
    artifact_path: Path
    token: str

    def assert_owned(self, artifact_path: Path) -> None:
        try:
            payload = json.loads(self.path.read_bytes())
        except (OSError, ValueError) as exc:
            raise RuntimeError("calibrator publication lease is no longer owned") from exc
        if (
            not isinstance(payload, dict)
            or artifact_path.resolve() != self.artifact_path
            or payload.get("token") != self.token
        ):
            raise RuntimeError("calibrator publication lease is no longer owned")


@contextmanager
def calibrator_lease(artifact_path: Path) -> Iterator[CalibratorLease]:
    """Exclusive cross-process ownership; live owners are never evicted by age."""
    target = artifact_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(f".{target.name}.training.lock")
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("calibrator training/publication is already running") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"token": token, "pid": os.getpid()}, stream)
        yield CalibratorLease(lock, target, token)
    finally:
        try:
            payload = json.loads(lock.read_bytes())
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict) and payload.get("token") == token:
            lock.unlink(missing_ok=True)


def _load_frozen_schema(inputs: InputSnapshot, path: str | Path) -> dict[str, dict[str, Any]]:
    content = inputs.read(path)
    if content is None:
        return {}
    payload = json.loads(content)
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
        raise ValueError("Schema snapshot must contain complete tables")
    schema = require_schema_contract(payload["tables"])
    if payload.get("schema_version") != f"sha256:{schema_fingerprint(schema)}":
        raise ValueError("Schema snapshot fingerprint mismatch")
    return schema


async def _pin_models(
    inputs: InputSnapshot, config: Settings, runtime: RuntimeResources
) -> dict[str, str]:
    if is_production(config) and (
        len(config.llm_model_digest) < 32 or len(config.embedding_model_digest) < 32
    ):
        raise ValueError("production retrieval training requires configured model digest pins")
    probe = getattr(runtime, "probe_ollama", None)
    digests = getattr(runtime, "model_digests", None)
    if not callable(probe) or not callable(digests):
        if is_production(config):
            raise ValueError("production retrieval training requires model digest verification")
        return {}

    def observe() -> dict[str, str]:
        probe()
        available = digests()
        result = {}
        for model, expected in (
            (config.llm_model, config.llm_model_digest),
            (config.embedding_model, config.embedding_model_digest),
        ):
            actual = available.get(model) or available.get(f"{model}:latest")
            if not isinstance(actual, str) or not actual or (expected and actual != expected):
                raise ValueError(f"retrieval training model digest unavailable or changed: {model}")
            result[model] = actual
        return result

    identity = await asyncio.to_thread(observe)
    inputs.pin_identity("models", identity, observe)
    return identity


async def train_table_retriever(config: Settings, runtime: RuntimeResources) -> dict:
    with calibrator_lease(Path(config.table_retrieval_calibrator_path)) as lease:
        return await _train_table_retriever(config, runtime, lease)


async def _train_table_retriever(
    config: Settings, runtime: RuntimeResources, lease: CalibratorLease
) -> dict:
    inputs = InputSnapshot()
    inputs.read(config.table_retrieval_calibrator_path)
    context_state = ContextRuntimeState()
    requested_source = config.table_retrieval_train_schema_source
    if is_production(config):
        if requested_source == "snapshot":
            raise ValueError("production retrieval training requires live authoritative Schema")
        requested_source = "live"
    if requested_source not in {"auto", "live", "snapshot"}:
        raise ValueError("TABLE_RETRIEVAL_TRAIN_SCHEMA_SOURCE 必须是 auto、live 或 snapshot")
    schema_source = "live_database"
    schema = {}
    live_error: Exception | None = None
    if requested_source != "snapshot":
        executor = VannaSqlExecutor(runtime.sql_runner, max_concurrency=config.sql_max_concurrency)
        try:
            schema = await get_live_schema(
                config.schema_cache_ttl_seconds,
                force_refresh=True,
                sql_executor=executor,
                state=context_state,
            )
        except Exception as exc:
            live_error = exc
            if requested_source == "live":
                raise
        finally:
            await executor.aclose()
    if not schema:
        schema = _load_frozen_schema(inputs, config.schema_snapshot_path)
        schema_source = "versioned_schema_snapshot"
        if not schema:
            raise RuntimeError(
                "实时 Schema 不可用，且 SCHEMA_SNAPSHOT_PATH 没有可用的离线结构快照"
            ) from live_error
    require_schema_contract(schema)
    inputs.pin_identity("schema", schema_fingerprint(schema), lambda: schema_fingerprint(schema))
    inputs.pin_directory(config.structured_knowledge_dir)
    bundle = load_validated_knowledge_bundle(
        config.structured_knowledge_dir,
        schema,
        require_reviewed=is_production(config),
        reader=inputs.read,
    )
    if isinstance(bundle, KnowledgeBundle):
        inputs.pin_identity("knowledge", bundle.fingerprint, lambda: bundle.fingerprint)
    model_identity = await _pin_models(inputs, config, runtime)
    semantic_catalog = bundle.semantic_catalog()
    paths = {
        "retrieval_train": config.retrieval_train_set_path,
        "retrieval_calibration": config.retrieval_calibration_set_path,
        "retrieval_test": config.retrieval_test_set_path,
        "dev": config.eval_dev_set_path,
        "test": config.eval_test_set_path,
    }
    assert_training_isolation(
        paths,
        bundle.gold_sql + bundle.refusals,
        negative_records=bundle.negative_sql,
        reader=inputs.read,
    )
    if is_production(config):
        errors = configured_generation_errors(
            paths,
            config.structured_knowledge_dir,
            reader=inputs.read,
        )
        if errors:
            raise ValueError("; ".join(errors))
    assert_disjoint_splits(
        *(
            load_evaluation_cases_bytes(
                inputs.require_read(path), source=path, expected_split=split
            )
            for path, split in (
                (config.retrieval_train_set_path, "retrieval_train"),
                (config.retrieval_calibration_set_path, "retrieval_calibration"),
                (config.retrieval_test_set_path, "retrieval_test"),
            )
        )
    )
    train_examples = load_retrieval_examples(config.retrieval_train_set_path, reader=inputs.read)
    calibration_examples = load_retrieval_examples(
        config.retrieval_calibration_set_path,
        expected_split="retrieval_calibration",
        reader=inputs.read,
    )
    test_examples = load_retrieval_examples(
        config.retrieval_test_set_path,
        expected_split="retrieval_test",
        reader=inputs.read,
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
        business_cards=bundle.table_cards,
    )

    async def check_live_schema() -> None:
        if schema_source != "live_database":
            return
        executor = VannaSqlExecutor(runtime.sql_runner, max_concurrency=config.sql_max_concurrency)
        try:
            latest = await get_live_schema(
                config.schema_cache_ttl_seconds,
                force_refresh=True,
                sql_executor=executor,
                state=context_state,
            )
        finally:
            await executor.aclose()
        if schema_fingerprint(latest) != schema_fingerprint(schema):
            raise InputSnapshotError("authoritative Schema changed before calibrator publication")

    try:
        return await _fit_and_evaluate(
            config,
            schema,
            bundle,
            semantic_catalog,
            retriever,
            train_examples,
            calibration_examples,
            test_examples,
            schema_source,
            inputs=inputs,
            lease=lease,
            embedding_model_digest=model_identity.get(config.embedding_model, ""),
            pre_publish_schema=check_live_schema,
        )
    finally:
        await retriever.aclose()


async def _fit_and_evaluate(
    config: Settings,
    schema: dict[str, dict[str, Any]],
    bundle: KnowledgeBundle,
    semantic_catalog: SemanticCatalog,
    retriever: TableRetriever,
    train_examples: list[RetrievalExample],
    calibration_examples: list[RetrievalExample],
    test_examples: list[RetrievalExample],
    schema_source: str,
    *,
    inputs: InputSnapshot,
    lease: CalibratorLease,
    embedding_model_digest: str,
    pre_publish_schema: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    async def score_examples(examples):
        lookup: dict[tuple[str, str], float] = {}
        semantic_grounding: dict[str, bool] = {}
        scored_by_question: dict[str, list] = {}
        semantic_irs: dict[str, QueryPlan] = {}
        for example in examples:
            semantic_ir = parse_question_semantics(example.question, semantic_catalog)
            semantic_irs[example.question] = semantic_ir
            semantic_grounding[example.question] = semantic_ir.is_grounded
            unknown = set(example.positive_tables) - set(schema)
            if unknown:
                raise ValueError(f"召回样本引用未知表: {example.question}: {sorted(unknown)}")
            scored_by_question[example.question] = await retriever.score_all(
                example.question, semantic_ir, schema
            )
            for card, score in scored_by_question[example.question]:
                lookup[(example.question, card.table_name)] = score
        records = serialize_pair_records(examples, schema)
        for record in records:
            record["raw_score"] = lookup[(record["question"], record["table_name"])]
            record["semantic_grounded"] = semantic_grounding[record["question"]]
        return records, scored_by_question, semantic_irs

    records, _, _ = await score_examples(train_examples)
    calibration_records, _, _ = await score_examples(calibration_examples)
    _, test_scores_by_question, test_semantic_irs = await score_examples(test_examples)
    fitted = mine_training_pairs(records)
    scores = [float(item["raw_score"]) for item in fitted]
    labels = [int(item["label"]) for item in fitted]
    class_counts = {label: labels.count(label) for label in set(labels)}
    calibrator = PlattCalibrator.fit(
        scores,
        labels,
        target_recall=0.99,
        sample_weights=[1.0 / class_counts[label] for label in labels],
    )
    calibrator = calibrator.calibrate_threshold(
        [float(item["raw_score"]) for item in calibration_records],
        [int(item["label"]) for item in calibration_records],
    )
    dataset_digest = retrieval_dataset_fingerprint(
        config.retrieval_train_set_path,
        config.retrieval_calibration_set_path,
        config.retrieval_test_set_path,
        reader=inputs.read,
    )
    calibrator = calibrator.with_provenance(
        schema_fingerprint=schema_fingerprint(schema),
        embedding_model=config.embedding_model,
        dataset_fingerprint=dataset_digest,
        business_card_fingerprint=business_cards_fingerprint(bundle.table_cards),
        embedding_model_digest=embedding_model_digest,
    )
    artifact_path = Path(config.table_retrieval_calibrator_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex
    dataset_path = artifact_path.with_name(f"{artifact_path.stem}.dataset.{run_id}.jsonl")
    report_path = artifact_path.with_name(f"{artifact_path.stem}.report.{run_id}.json")
    policy_report = evaluate_retrieval_policy(
        test_examples,
        test_semantic_irs,
        schema,
        test_scores_by_question,
        calibrator,
        token_budget=config.table_retrieval_token_budget,
        require_calibration=config.table_retrieval_require_calibration,
        business_cards=bundle.table_cards,
    )
    false_positive_rate = policy_report["negative_false_positive_rate"]
    test_recall = policy_report["held_out_table_recall"]
    accepted = (
        test_recall >= calibrator.target_recall
        and false_positive_rate <= config.table_retrieval_max_false_positive_rate
        and policy_report["refusal_pass_rate"] == 1.0
    )
    report = {
        "train_examples": len(train_examples),
        "calibration_examples": len(calibration_examples),
        "test_examples": len(test_examples),
        "schema_source": schema_source,
        "pairs": len(records),
        "positive_pairs": sum(int(item["label"]) for item in records),
        "negative_pairs": sum(not item["label"] for item in records),
        "fit_pairs": len(fitted),
        "fit_hard_negative_pairs": sum(bool(item["hard_negative"]) for item in fitted),
        "business_card_fingerprint": calibrator.business_card_fingerprint,
        "schema_fingerprint": calibrator.schema_fingerprint,
        "dataset_fingerprint": calibrator.dataset_fingerprint,
        "embedding_model_digest": calibrator.embedding_model_digest,
        "threshold": calibrator.threshold,
        "target_recall": calibrator.target_recall,
        "artifact_path": str(artifact_path),
        "dataset_path": str(dataset_path),
        **policy_report,
        "calibrator_accepted": accepted,
        "publication_status": "candidate",
        "report_path": str(report_path),
    }
    # Diagnostics are immutable per run and prepared before the model commit.
    # A reporting failure can therefore never replace the last-known-good model.
    atomic_bytes(
        dataset_path,
        (
            "\n".join(json.dumps(item, ensure_ascii=False, allow_nan=False) for item in records)
            + "\n"
        ).encode("utf-8"),
    )
    atomic_json(report_path, report)
    await pre_publish_schema()
    publication = await asyncio.to_thread(
        publish_calibrator_candidate,
        calibrator,
        artifact_path,
        accepted=accepted,
        table_recall=test_recall,
        false_positive_rate=false_positive_rate,
        maximum_false_positive_rate=config.table_retrieval_max_false_positive_rate,
        lease=lease,
        pre_publish=inputs.assert_unchanged,
    )
    return {**report, **publication, "publication_status": "accepted" if accepted else "rejected"}


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
