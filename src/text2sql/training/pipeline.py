"""Build and Dev-evaluate a candidate; formal promotion belongs to release."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from vanna import ToolContext, User

from ..application.context_state import ContextRuntimeState
from ..application.ports import SqlExecutor
from ..core.config import Settings, load_settings
from ..core.logging import setup_logging
from ..core.production import is_production
from ..evaluation import evaluate_quality_gate, summarize_evaluation
from ..evaluation.gold_set import assert_training_isolation, configured_generation_errors
from ..infrastructure.feedback_repository import FeedbackPolicy, SQLiteFeedbackRepository
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..infrastructure.schema_repository import get_live_schema
from ..knowledge import load_validated_knowledge_bundle
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.atomic import atomic_bytes
from ..knowledge.collection_evidence import assert_collection_evidence
from ..knowledge.input_snapshot import InputSnapshot
from ..knowledge.provenance import schema_fingerprint
from ..knowledge.snapshot import ArtifactSnapshot
from ..retrieval.calibrator import PlattCalibrator
from ..retrieval.dataset import retrieval_dataset_fingerprint
from ..retrieval.table_card import business_cards_fingerprint
from .fingerprint import (
    build_training_fingerprint,
    configured_training_tables,
    hash_file,
    hash_live_schema,
    should_skip_training,
    write_training_state,
)
from .knowledge_builder import train_feedback_examples, train_join_paths, train_structured_knowledge
from .profiling import train_sample_profiles
from .records import build_table_schema_index_records, dedupe_index_records
from .reporting import build_training_manifest, empty_training_report, finalize_training_report
from .storage import flush_knowledge_index, now_iso, save_training_text, write_json_file

logger = setup_logging("text2sql.training")


def train_knowledge(
    include_samples: bool = True,
    sample_rows: int = 10,
    *,
    config: Settings | None = None,
) -> None:
    """Run the complete offline knowledge build in a fresh event loop."""
    active_config = config or load_settings()
    registry = KnowledgeArtifactRegistry(
        active_config.knowledge_artifact_dir,
        active_config.knowledge_active_pointer_path,
    )
    lease = registry.acquire_training_lease(
        stale_seconds=active_config.knowledge_training_lock_stale_seconds
    )
    try:
        runtime = RuntimeResources(active_config)
    except BaseException:
        lease.release()
        raise
    context_state = ContextRuntimeState()
    try:
        asyncio.run(
            train_knowledge_async(
                active_config,
                runtime,
                include_samples,
                sample_rows,
                context_state,
            )
        )
    finally:
        try:
            removed = registry.prune(
                retain_count=active_config.knowledge_artifact_retention_count,
                delete_collection=runtime.delete_knowledge_collection,
            )
            if removed:
                logger.info("已清理历史知识版本: %s", ", ".join(removed))
        except Exception as exc:
            logger.warning("清理历史知识版本失败: %s", exc)
        try:
            runtime.close()
        finally:
            lease.release()


async def train_knowledge_async(
    config: Settings,
    runtime: RuntimeResources,
    include_samples: bool,
    sample_rows: int,
    context_state: ContextRuntimeState,
) -> None:
    """Own SQL worker lifetime for both CLI and programmatic training."""
    executor = VannaSqlExecutor(runtime.sql_runner, max_concurrency=config.sql_max_concurrency)
    try:
        await build_candidate(
            config, runtime, executor, include_samples, sample_rows, context_state
        )
    finally:
        try:
            await executor.aclose()
        finally:
            context_state.reset()


async def build_candidate(
    config: Settings,
    runtime: RuntimeResources,
    sql_executor: SqlExecutor,
    include_samples: bool,
    sample_rows: int,
    context_state: ContextRuntimeState,
) -> None:
    """Stage a versioned candidate without changing the serving pointer."""
    from ..evaluation.wiring import run_configured_evaluation

    logger.info("=== 开始知识索引构建 ===")
    live_schema = await get_live_schema(
        config.schema_cache_ttl_seconds,
        force_refresh=True,
        sql_executor=sql_executor,
        state=context_state,
    )
    allowed_tables = configured_training_tables(config)
    inputs = InputSnapshot()
    knowledge_files = inputs.pin_directory(config.structured_knowledge_dir)
    for path in (
        config.retrieval_train_set_path,
        config.retrieval_calibration_set_path,
        config.retrieval_test_set_path,
        config.eval_dev_set_path,
        config.eval_test_set_path,
        config.table_retrieval_calibrator_path,
    ):
        inputs.require_read(path)
    if callable(getattr(runtime, "model_digests", None)):
        if callable(getattr(runtime, "probe_ollama", None)):
            await asyncio.to_thread(runtime.probe_ollama)
        observed_models = await asyncio.to_thread(runtime.model_digests)
        inputs.pin_identity("ollama_models", observed_models, runtime.model_digests)
    elif is_production(config):
        raise RuntimeError("production training requires observed model identities")
    else:
        observed_models = {}
    feedback_repository = SQLiteFeedbackRepository(
        config.feedback_db_path,
        FeedbackPolicy(
            enabled=config.enable_feedback_capture,
            require_execution_success=config.feedback_require_execution_success,
            require_nonempty_result=config.feedback_require_nonempty_result,
            min_result_rows=config.feedback_min_result_rows,
            min_quality_score=config.feedback_min_quality_score,
        ),
    )
    gold_feedback = await asyncio.to_thread(
        feedback_repository.load_gold, current_schema_fingerprint=schema_fingerprint(live_schema)
    )
    raw_gold_count = await asyncio.to_thread(feedback_repository.count, "gold")
    if is_production(config) and gold_feedback:
        logger.warning(
            "生产训练不直接吸收运行反馈；请导入 canonical Gold Set 并完成双人审核与执行证据"
        )
        gold_feedback = []
    fingerprint = build_training_fingerprint(
        config,
        include_samples=include_samples,
        sample_rows=sample_rows,
        live_schema_hash=hash_live_schema(live_schema),
        gold_feedback=gold_feedback,
        reader=inputs.read,
        knowledge_files=knowledge_files,
    )
    bundle = load_validated_knowledge_bundle(
        config.structured_knowledge_dir,
        live_schema,
        require_reviewed=is_production(config),
        reader=inputs.read,
    )
    assert_training_isolation(
        {
            "retrieval_train": config.retrieval_train_set_path,
            "retrieval_calibration": config.retrieval_calibration_set_path,
            "retrieval_test": config.retrieval_test_set_path,
            "dev": config.eval_dev_set_path,
            "test": config.eval_test_set_path,
        },
        bundle.gold_sql + bundle.refusals + gold_feedback,
        negative_records=bundle.negative_sql,
        reader=inputs.read,
    )
    if is_production(config):
        errors = configured_generation_errors(
            {
                "retrieval_train": config.retrieval_train_set_path,
                "retrieval_calibration": config.retrieval_calibration_set_path,
                "retrieval_test": config.retrieval_test_set_path,
                "dev": config.eval_dev_set_path,
                "test": config.eval_test_set_path,
            },
            config.structured_knowledge_dir,
            reader=inputs.read,
        )
        if errors:
            raise ValueError("; ".join(errors))
    if not bundle.available:
        raise FileNotFoundError(f"结构化知识库不存在或为空: {config.structured_knowledge_dir}")
    if bundle.errors:
        preview = "；".join(bundle.errors[:8])
        raise ValueError(f"结构化知识预检查失败，训练未修改现有知识库: {preview}")
    inputs.assert_unchanged()
    if should_skip_training(config, fingerprint):
        logger.info("训练输入未变化，保留已验证的候选版本。")
        return

    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    )
    candidate = registry.candidate(schema_fingerprint=schema_fingerprint(live_schema))
    source_calibrator = Path(config.table_retrieval_calibrator_path)
    calibrator_bytes = inputs.require_read(source_calibrator)
    retrieval_digest = retrieval_dataset_fingerprint(
        config.retrieval_train_set_path,
        config.retrieval_calibration_set_path,
        config.retrieval_test_set_path,
        reader=inputs.read,
    )
    embedding_digest = observed_models.get(config.embedding_model) or observed_models.get(
        f"{config.embedding_model}:latest"
    )
    if is_production(config) and not embedding_digest:
        raise RuntimeError("production training requires the observed embedding digest")
    calibrator = PlattCalibrator.from_bytes(
        calibrator_bytes,
        expected_schema_fingerprint=candidate.schema_fingerprint,
        expected_embedding_model=config.embedding_model,
        expected_embedding_model_digest=embedding_digest,
        expected_business_card_fingerprint=business_cards_fingerprint(bundle.table_cards),
        expected_dataset_fingerprint=retrieval_digest,
    )
    if calibrator is None:
        raise RuntimeError("召回器校准产物缺失或已过期，请先运行 text2sql-train-retriever")
    write_json_file(
        candidate.snapshot_path,
        ArtifactSnapshot(
            live_schema,
            bundle,
            retrieval_digest,
        ).to_dict(),
    )
    Path(candidate.calibrator_path).parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(candidate.calibrator_path, calibrator_bytes)

    index_records: list[dict[str, Any]] = []
    report = empty_training_report(
        config,
        include_samples,
        sample_rows,
        started_at=now_iso(),
    )

    knowledge_memory = runtime.create_knowledge_memory(collection_name=candidate.collection_name)

    ctx = ToolContext(
        user=User(id="trainer", username="admin"),
        conversation_id="training",
        request_id="train",
        agent_memory=knowledge_memory,
    )

    logger.info(
        "正在构建候选知识版本 %s；活动版本继续提供服务...",
        candidate.version,
    )

    build_table_schema_index_records(live_schema, index_records, report, allowed_tables)

    if include_samples:
        await train_sample_profiles(
            executor=sql_executor,
            knowledge_memory=knowledge_memory,
            context=ctx,
            sample_rows=sample_rows,
            index_records=index_records,
            report=report,
            live_schema=live_schema,
            config=config,
            allowed_tables=allowed_tables,
            save_training_text=save_training_text,
        )

    await train_join_paths(live_schema, knowledge_memory, ctx, index_records, allowed_tables)

    await train_structured_knowledge(
        knowledge_memory, ctx, bundle, index_records, report, allowed_tables
    )

    await train_feedback_examples(
        knowledge_memory,
        ctx,
        index_records,
        report,
        bundle,
        gold_feedback,
        raw_gold_count,
        allowed_tables,
    )

    index_records, removed_count = dedupe_index_records(index_records)
    report["deduped_records_removed"] = removed_count
    flush_knowledge_index(index_records, candidate.knowledge_index_path)
    collection = await asyncio.to_thread(
        runtime.collection_evidence, candidate.collection_name, index_records=index_records
    )
    report = finalize_training_report(report, index_records, finished_at=now_iso())
    evaluations = await run_configured_evaluation(
        config,
        runtime,
        config.training_eval_split,
        knowledge_memory=knowledge_memory,
        knowledge_index_path=candidate.knowledge_index_path,
        calibrator_path=candidate.calibrator_path,
        dataset_bytes=inputs.require_read(config.eval_dev_set_path),
    )
    report["evaluations"] = evaluations
    report["evaluation_split"] = config.training_eval_split
    report["evaluation_summary"] = summarize_evaluation(evaluations)
    quality_gate = evaluate_quality_gate(
        report["evaluation_summary"],
        min_pass_rate=config.eval_min_pass_rate,
        min_positive_pass_rate=config.eval_min_positive_pass_rate,
        min_refusal_pass_rate=config.eval_min_refusal_pass_rate,
        min_cases=config.eval_min_cases,
        min_positive_cases=config.eval_min_positive_cases,
        min_refusal_cases=config.eval_min_refusal_cases,
        min_semantic_ir_pass_rate=config.eval_min_semantic_ir_pass_rate,
        min_execution_pass_rate=config.eval_min_execution_pass_rate,
        min_retrieval_recall=config.eval_min_retrieval_recall,
    )
    report["quality_gate"] = quality_gate
    report["artifact_version"] = candidate.version
    report["artifact_status"] = "candidate" if quality_gate["passed"] else "rejected"
    report["collection_evidence"] = collection
    write_json_file(candidate.report_path, report)
    write_json_file(
        candidate.manifest_path,
        build_training_manifest(
            generated_at=now_iso(),
            include_samples=include_samples,
            sample_rows=sample_rows,
            training_tables=sorted(configured_training_tables(config) or []),
            index_records=index_records,
            report=report,
            fingerprint=fingerprint,
            artifact_hashes={
                "knowledge_index_sha256": hash_file(candidate.knowledge_index_path),
                "knowledge_snapshot_sha256": hash_file(candidate.snapshot_path),
                "calibrator_sha256": hash_file(candidate.calibrator_path),
                "training_report_sha256": hash_file(candidate.report_path),
                "collection_evidence": collection,
            },
        ),
    )
    if not quality_gate["passed"]:
        raise RuntimeError(
            "候选知识版本未通过 Dev 质量门禁，线上版本保持不变: "
            + "；".join(quality_gate["failures"])
        )

    current_schema = await get_live_schema(
        config.schema_cache_ttl_seconds,
        force_refresh=True,
        sql_executor=sql_executor,
        state=context_state,
    )
    if schema_fingerprint(current_schema) != candidate.schema_fingerprint:
        raise RuntimeError("database Schema changed during knowledge build")
    assert_collection_evidence(
        await asyncio.to_thread(
            runtime.collection_evidence, candidate.collection_name, index_records=index_records
        ),
        collection,
    )
    inputs.assert_unchanged()
    registry.stage(candidate)
    write_training_state(config, fingerprint, report)
    context_state.reset()

    logger.info("=== 候选知识版本 %s 已登记；尚未切换 ACTIVE ===", candidate.version)
