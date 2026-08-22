"""Build offline knowledge artifacts and run isolated evaluation.

The pipeline snapshots live Schema metadata, profiles selected columns, writes
typed semantic memories, imports approved Gold feedback, deduplicates sidecar
records, persists manifests, and evaluates either the development or frozen
test split. Markdown documents are never used as training input.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vanna import ToolContext, User

from ..application.context_state import ContextRuntimeState
from ..application.schema_repository import get_live_schema
from ..core.build_info import release_identity
from ..core.config import Settings, load_settings
from ..core.logging import setup_logging
from ..domain.semantic_ir import SemanticCatalog, parse_question_semantics
from ..evaluation import evaluate_quality_gate, summarize_evaluation
from ..infrastructure.feedback_repository import FeedbackPolicy, SQLiteFeedbackRepository
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..knowledge import KnowledgeBundle, load_validated_knowledge_bundle
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.provenance import schema_fingerprint
from ..retrieval.calibrator import PlattCalibrator
from ..retrieval.dataset import retrieval_dataset_fingerprint
from .profiling import train_sample_profiles
from .reporting import (
    build_training_manifest,
    empty_training_report,
    finalize_training_report,
)

logger = setup_logging("text2sql.training")
TRAINING_FINGERPRINT_VERSION = 6
TRAINING_PRIORITY = {
    "feedback_example": 100,
    "question_sql_example": 95,
    "structured_question": 92,
    "analogy_rule": 91,
    "join_path": 90,
    "question_template": 89,
    "table_role": 88,
    "metric_rule": 86,
    "time_rule": 84,
    "synonym_rule": 82,
    "alias_dict": 80,
    "field_alias": 78,
    "table_alias": 76,
    "column_schema": 72,
    "table_description": 70,
    "foreign_key": 68,
    "column_profile": 66,
    "sample_values": 64,
    "table_card": 90,
    "domain_policy": 88,
    "dimension_rule": 86,
    "refusal_example": 90,
    "negative_sql_example": 90,
}


def _build_index_record(
    text: str,
    source_type: str,
    table_names: list[str] | None = None,
    field_names: list[str] | None = None,
    aliases: list[str] | None = None,
    metric_tags: list[str] | None = None,
    dimension_tags: list[str] | None = None,
    time_tags: list[str] | None = None,
    filter_tags: list[str] | None = None,
    join_tables: list[str] | None = None,
    role_tags: list[str] | None = None,
    profile_tags: list[str] | None = None,
    enum_values: list[str] | None = None,
    granularity: str | None = None,
    confidence: int = 50,
) -> dict[str, Any]:
    return {
        "content": text.strip(),
        "source_type": source_type,
        "table_names": _dedupe_keep_order(table_names or []),
        "field_names": _dedupe_keep_order(field_names or []),
        "aliases": _dedupe_keep_order(aliases or []),
        "metric_tags": _dedupe_keep_order(metric_tags or []),
        "dimension_tags": _dedupe_keep_order(dimension_tags or []),
        "time_tags": _dedupe_keep_order(time_tags or []),
        "filter_tags": _dedupe_keep_order(filter_tags or []),
        "join_tables": _dedupe_keep_order(join_tables or []),
        "role_tags": _dedupe_keep_order(role_tags or []),
        "profile_tags": _dedupe_keep_order(profile_tags or []),
        "enum_values": _dedupe_keep_order(enum_values or []),
        "granularity": (granularity or "").strip(),
        "priority": TRAINING_PRIORITY.get(source_type, 50),
        "confidence": confidence,
    }


def _flush_knowledge_index(records: list[dict[str, Any]], index_path_value: str) -> None:
    index_path = Path(index_path_value)
    _atomic_write_text(index_path, json.dumps(records, ensure_ascii=False, indent=2))
    logger.info("知识索引已写入: %s (%d 条)", index_path, len(records))


def _append_index_record(
    index_records: list[dict[str, Any]],
    text: str,
    *,
    source_type: str,
    table_names: list[str] | None = None,
    field_names: list[str] | None = None,
    aliases: list[str] | None = None,
    metric_tags: list[str] | None = None,
    dimension_tags: list[str] | None = None,
    time_tags: list[str] | None = None,
    filter_tags: list[str] | None = None,
    join_tables: list[str] | None = None,
    role_tags: list[str] | None = None,
    profile_tags: list[str] | None = None,
    enum_values: list[str] | None = None,
    granularity: str | None = None,
    confidence: int = 50,
) -> None:
    content = text.strip()
    if not content:
        return
    index_records.append(
        _build_index_record(
            content,
            source_type=source_type,
            table_names=table_names,
            field_names=field_names,
            aliases=aliases,
            metric_tags=metric_tags,
            dimension_tags=dimension_tags,
            time_tags=time_tags,
            filter_tags=filter_tags,
            join_tables=join_tables,
            role_tags=role_tags,
            profile_tags=profile_tags,
            enum_values=enum_values,
            granularity=granularity,
            confidence=confidence,
        )
    )


async def _save_training_text(
    knowledge_memory,
    text: str,
    ctx,
    index_records: list[dict[str, Any]],
    source_type: str,
    table_names: list[str] | None = None,
    field_names: list[str] | None = None,
    aliases: list[str] | None = None,
    metric_tags: list[str] | None = None,
    dimension_tags: list[str] | None = None,
    time_tags: list[str] | None = None,
    filter_tags: list[str] | None = None,
    join_tables: list[str] | None = None,
    role_tags: list[str] | None = None,
    profile_tags: list[str] | None = None,
    enum_values: list[str] | None = None,
    granularity: str | None = None,
    confidence: int = 50,
) -> None:
    content = text.strip()
    if not content:
        return

    await knowledge_memory.save_text_memory(content, ctx)
    _append_index_record(
        index_records,
        content,
        source_type=source_type,
        table_names=table_names,
        field_names=field_names,
        aliases=aliases,
        metric_tags=metric_tags,
        dimension_tags=dimension_tags,
        time_tags=time_tags,
        filter_tags=filter_tags,
        join_tables=join_tables,
        role_tags=role_tags,
        profile_tags=profile_tags,
        enum_values=enum_values,
        granularity=granularity,
        confidence=confidence,
    )


def _dedupe_keep_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _first_sentence(text: str) -> str:
    return re.split(r"[。；;，,]", text.strip(), maxsplit=1)[0].strip()


def _field_aliases(description: str) -> list[str]:
    core = _first_sentence(description)
    aliases = [core]

    if core.endswith("名称"):
        aliases.append(core[:-2] + "名")
    if core.endswith("编号"):
        aliases.append(core[:-2] + "ID")
    if core.endswith("日期"):
        aliases.append(core[:-2] + "时间")
    if core.startswith("是否"):
        aliases.append(core[2:])

    return _dedupe_keep_order(aliases)


def _question_sql_metadata(
    question: str,
    sql: str,
    catalog: SemanticCatalog,
    allowed_tables: set[str] | None,
) -> dict[str, Any]:
    semantic_ir = parse_question_semantics(question, catalog)
    sql_tables = [
        table
        for table in _extract_sql_table_names(sql)
        if _should_include_table(table, allowed_tables)
    ]
    return {
        "dimensions": list(semantic_ir.dimensions),
        "metric": ",".join(metric.name for metric in semantic_ir.metrics) or "未识别",
        "time_expression": (
            f"{semantic_ir.date_start} ~ {semantic_ir.date_end}"
            if semantic_ir.date_start and semantic_ir.date_end
            else "未显式说明"
        ),
        "filters": [
            f"{intent.entity}:{value}"
            for intent in semantic_ir.entity_filter_intents
            for value in intent.values
        ],
        "sql_tables": sql_tables,
    }


def _normalize_training_question(question: str) -> str:
    question = re.sub(r"\s+", " ", str(question or "")).strip()
    question = re.sub(r"^[0-9]+[\.\-、:：\s]+", "", question)
    question = re.sub(r"[?？。！!；;]+$", "", question).strip()
    return question


def _question_aliases(question: str) -> list[str]:
    normalized = _normalize_training_question(question)
    aliases = [str(question or "").strip(), normalized]
    if normalized:
        aliases.append(f"{normalized}?")
        aliases.append(f"{normalized}？")
    return _dedupe_keep_order([alias for alias in aliases if alias])


def _extract_sql_table_names(sql: str) -> list[str]:
    matches = re.findall(
        r"(?is)\b(?:from|join|update|into)\s+((?:\[[^\]]+\]|\w+)(?:\.(?:\[[^\]]+\]|\w+))*)",
        sql,
    )
    table_names: list[str] = []
    for match in matches:
        parts = [part.strip("[]") for part in match.split(".")]
        if parts:
            table_names.append(parts[-1])
    return _dedupe_keep_order(table_names)


def _metric_tags_from_metadata(metadata: dict[str, Any]) -> list[str] | None:
    metric = str(metadata.get("metric") or "").strip()
    return [metric] if metric and metric != "未识别" else None


def _time_tags_from_metadata(metadata: dict[str, Any]) -> list[str] | None:
    time_expression = str(metadata.get("time_expression") or "").strip()
    return [time_expression] if time_expression and time_expression != "未显式说明" else None


async def _train_question_sql_examples(
    knowledge_memory,
    ctx,
    question_pairs: list[tuple[str, str]],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    catalog: SemanticCatalog,
    allowed_tables: set[str] | None,
) -> None:
    report["question_sql_examples_trained"] = len(question_pairs)
    for question, sql in question_pairs:
        metadata = _question_sql_metadata(question, sql, catalog, allowed_tables)
        example_text = f"中文问题与 SQL 示例:\n问题: {question}\nSQL Server SQL:\n{sql}"
        await _save_training_text(
            knowledge_memory,
            example_text,
            ctx,
            index_records,
            source_type="question_sql_example",
            table_names=metadata["sql_tables"],
            aliases=_question_aliases(question),
            metric_tags=_metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=_time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            join_tables=metadata["sql_tables"],
            confidence=95,
        )


def _configured_training_tables(config: Settings) -> set[str] | None:
    if not config.training_tables:
        return None
    return {item.strip() for item in config.training_tables.split(",") if item.strip()}


def _should_include_table(table_name: str, allowed_tables: set[str] | None) -> bool:
    return allowed_tables is None or table_name in allowed_tables


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_json_file(path_str: str, payload: Any) -> None:
    path = Path(path_str)
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json_file(path_str: str) -> dict[str, Any] | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 JSON 文件失败 %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path_str: str | None) -> str:
    if not path_str:
        return ""
    path = Path(path_str)
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_directory(path_str: str) -> str:
    root = Path(path_str)
    if not root.exists():
        return ""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _hash_live_schema(live_schema: dict[str, dict[str, Any]]) -> str:
    serialized = json.dumps(live_schema, ensure_ascii=False, sort_keys=True)
    return _hash_text(serialized)


def _build_training_fingerprint(
    config: Settings,
    *,
    include_samples: bool,
    sample_rows: int,
    live_schema_hash: str,
) -> dict[str, Any]:
    return {
        "version": TRAINING_FINGERPRINT_VERSION,
        "include_samples": bool(include_samples),
        "sample_rows": int(sample_rows),
        "structured_knowledge_dir": config.structured_knowledge_dir,
        "structured_knowledge_hash": _hash_directory(config.structured_knowledge_dir),
        "live_schema_hash": live_schema_hash,
        "retrieval_train_set_hash": _hash_file(config.retrieval_train_set_path),
        "retrieval_calibration_set_hash": _hash_file(config.retrieval_calibration_set_path),
        "retrieval_test_set_hash": _hash_file(config.retrieval_test_set_path),
        "eval_dev_set_hash": _hash_file(config.eval_dev_set_path),
        "eval_test_set_hash": _hash_file(config.eval_test_set_path),
        "training_eval_split": config.training_eval_split,
        "feedback_database_hash": _hash_file(config.feedback_db_path),
        "table_retrieval_calibrator_hash": _hash_file(config.table_retrieval_calibrator_path),
        "training_tables": sorted(_configured_training_tables(config) or []),
        "sample_tables": sorted(
            item.strip() for item in (config.sample_tables or "").split(",") if item.strip()
        ),
        "profiling_max_tables": config.profiling_max_tables,
        "profiling_max_columns_per_table": config.profiling_max_columns_per_table,
        "profiling_max_distinct_values": config.profiling_max_distinct_values,
        "profiling_allowed_columns": config.profiling_allowed_columns,
        "profiling_denied_columns": config.profiling_denied_columns,
        "feedback_min_result_rows": config.feedback_min_result_rows,
        "feedback_require_nonempty_result": config.feedback_require_nonempty_result,
        "feedback_require_execution_success": config.feedback_require_execution_success,
        "priority_hash": _hash_text(json.dumps(TRAINING_PRIORITY, sort_keys=True)),
        "release_identity": release_identity(config),
    }


def _should_skip_training(config: Settings, fingerprint: dict[str, Any]) -> bool:
    if not config.training_skip_unchanged:
        return False
    state = _read_json_file(config.training_state_path)
    if not state:
        return False
    artifacts = [
        config.knowledge_active_pointer_path,
    ]
    if not all(Path(path).exists() for path in artifacts):
        return False
    return state.get("fingerprint") == fingerprint


def _write_training_state(
    config: Settings, fingerprint: dict[str, Any], report: dict[str, Any]
) -> None:
    payload = {
        "updated_at": _now_iso(),
        "fingerprint": fingerprint,
        "summary": {
            "table_count": report.get("table_count", 0),
            "column_count": report.get("column_count", 0),
            "knowledge_records": report.get("knowledge_records", 0),
            "memory_records": report.get("memory_records", 0),
            "evaluation_summary": report.get("evaluation_summary", {}),
        },
    }
    _write_json_file(config.training_state_path, payload)


def _merge_index_values(existing: list[str], incoming: list[str]) -> list[str]:
    return _dedupe_keep_order([*(existing or []), *(incoming or [])])


def _dedupe_index_records(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    ordered_keys: list[tuple[str, str]] = []

    for record in records:
        source_type = str(record.get("source_type") or "")
        content = str(record.get("content") or "").strip()
        if not source_type or not content:
            continue
        key = (source_type, content)
        if key not in deduped:
            deduped[key] = dict(record)
            ordered_keys.append(key)
            continue

        existing = deduped[key]
        for field in [
            "table_names",
            "field_names",
            "aliases",
            "metric_tags",
            "dimension_tags",
            "time_tags",
            "filter_tags",
            "join_tables",
            "role_tags",
            "profile_tags",
            "enum_values",
        ]:
            existing[field] = _merge_index_values(
                list(existing.get(field, []) or []),
                list(record.get(field, []) or []),
            )
        existing["priority"] = max(
            int(existing.get("priority", 0) or 0),
            int(record.get("priority", 0) or 0),
        )
        existing["confidence"] = max(
            int(existing.get("confidence", 0) or 0),
            int(record.get("confidence", 0) or 0),
        )
        if not existing.get("granularity") and record.get("granularity"):
            existing["granularity"] = record.get("granularity")

    deduped_records = [deduped[key] for key in ordered_keys]
    return deduped_records, max(0, len(records) - len(deduped_records))


def _build_table_schema_index_records(
    live_schema: dict[str, dict[str, Any]],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    allowed_tables: set[str] | None,
) -> None:
    table_count = 0
    column_count = 0
    for table_name, info in sorted(live_schema.items()):
        if not _should_include_table(table_name, allowed_tables):
            continue
        description = str(info.get("description") or "")
        columns = info.get("columns", {})
        field_names = list(columns.keys())
        aliases = [description] if description else None
        _append_index_record(
            index_records,
            f"表: {table_name} 描述: {description}",
            source_type="table_description",
            table_names=[table_name],
            aliases=aliases,
            confidence=72,
        )
        _append_index_record(
            index_records,
            "表名: {table_name}\n字段列表:\n{field_block}".format(
                table_name=table_name,
                field_block="\n".join(
                    "  - 字段: {name} ({dtype}, 可空:{nullable}){suffix}".format(
                        name=column_name,
                        dtype=column_info.get("data_type", ""),
                        nullable=column_info.get("is_nullable", False),
                        suffix=(
                            f" - {column_info.get('description', '')}"
                            if column_info.get("description")
                            else ""
                        ),
                    )
                    for column_name, column_info in columns.items()
                ),
            ),
            source_type="column_schema",
            table_names=[table_name],
            field_names=field_names,
            aliases=_dedupe_keep_order(
                [
                    alias
                    for column_info in columns.values()
                    for alias in _field_aliases(str(column_info.get("description", "")))
                    if column_info.get("description")
                ]
            ),
            confidence=82,
        )
        for fk in info.get("foreign_keys", []):
            referenced_table = str(fk.get("referenced_table") or "")
            if not referenced_table or not _should_include_table(referenced_table, allowed_tables):
                continue
            column_name = str(fk.get("column_name") or "")
            _append_index_record(
                index_records,
                f"外键关系: {table_name}.{column_name} -> {referenced_table}",
                source_type="foreign_key",
                table_names=[table_name, referenced_table],
                field_names=[column_name] if column_name else None,
                join_tables=[table_name, referenced_table],
                confidence=78,
            )
        table_count += 1
        column_count += len(field_names)
    report["table_count"] = table_count
    report["column_count"] = column_count


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
    runtime = RuntimeResources(active_config)
    context_state = ContextRuntimeState()
    try:
        asyncio.run(
            _train_async(
                active_config,
                runtime,
                include_samples,
                sample_rows,
                context_state,
            )
        )
    finally:
        lease.release()
        try:
            removed = registry.prune(
                retain_count=active_config.knowledge_artifact_retention_count,
                delete_collection=runtime.delete_knowledge_collection,
            )
            if removed:
                logger.info("已清理历史知识版本: %s", ", ".join(removed))
        except Exception as exc:
            logger.warning("清理历史知识版本失败: %s", exc)
        runtime.close()


async def _train_async(
    config: Settings,
    runtime: RuntimeResources,
    include_samples: bool,
    sample_rows: int,
    context_state: ContextRuntimeState,
) -> None:
    """Build, evaluate, and atomically publish a versioned knowledge artifact."""
    from ..evaluation.wiring import run_configured_evaluation

    logger.info("=== 开始知识索引构建 ===")
    sql_executor = VannaSqlExecutor(runtime.sql_runner, max_concurrency=config.sql_max_concurrency)
    live_schema = await get_live_schema(
        config.schema_cache_ttl_seconds,
        force_refresh=True,
        sql_executor=sql_executor,
        state=context_state,
    )
    allowed_tables = _configured_training_tables(config)
    fingerprint = _build_training_fingerprint(
        config,
        include_samples=include_samples,
        sample_rows=sample_rows,
        live_schema_hash=_hash_live_schema(live_schema),
    )
    if _should_skip_training(config, fingerprint):
        logger.info("训练输入未变化，跳过本次重训。")
        return

    bundle = load_validated_knowledge_bundle(config.structured_knowledge_dir, live_schema)
    if not bundle.available:
        raise FileNotFoundError(f"结构化知识库不存在或为空: {config.structured_knowledge_dir}")
    if bundle.errors:
        preview = "；".join(bundle.errors[:8])
        raise ValueError(f"结构化知识预检查失败，训练未修改现有知识库: {preview}")

    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    )
    candidate = registry.candidate(schema_fingerprint=schema_fingerprint(live_schema))
    source_calibrator = Path(config.table_retrieval_calibrator_path)
    calibrator = PlattCalibrator.load(
        source_calibrator,
        expected_schema_fingerprint=candidate.schema_fingerprint,
        expected_embedding_model=config.embedding_model,
        expected_dataset_fingerprint=retrieval_dataset_fingerprint(
            config.retrieval_train_set_path,
            config.retrieval_calibration_set_path,
            config.retrieval_test_set_path,
        ),
    )
    if calibrator is None:
        raise RuntimeError("召回器校准产物缺失或已过期，请先运行 text2sql-train-retriever")
    Path(candidate.calibrator_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_calibrator, candidate.calibrator_path)

    index_records: list[dict[str, Any]] = []
    report = empty_training_report(
        config,
        include_samples,
        sample_rows,
        started_at=_now_iso(),
    )

    knowledge_memory = runtime.create_knowledge_memory(collection_name=candidate.collection_name)

    agent_memory = runtime.agent_memory
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

    ctx = ToolContext(
        user=User(id="trainer", username="admin"),
        conversation_id="training",
        request_id="train",
        agent_memory=agent_memory,
    )

    logger.info(
        "正在构建候选知识版本 %s；保留线上知识与 Agent 对话记忆...",
        candidate.version,
    )

    _build_table_schema_index_records(live_schema, index_records, report, allowed_tables)

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
            save_training_text=_save_training_text,
        )

    await _train_join_paths(live_schema, knowledge_memory, ctx, index_records, allowed_tables)

    await _train_structured_knowledge(
        knowledge_memory, ctx, bundle, index_records, report, allowed_tables
    )

    await _train_feedback_examples(
        knowledge_memory,
        ctx,
        index_records,
        report,
        bundle,
        feedback_repository,
        schema_fingerprint(live_schema),
        allowed_tables,
    )

    index_records, removed_count = _dedupe_index_records(index_records)
    report["deduped_records_removed"] = removed_count
    _flush_knowledge_index(index_records, candidate.knowledge_index_path)
    report = finalize_training_report(report, index_records, finished_at=_now_iso())
    evaluations = await run_configured_evaluation(
        config,
        runtime,
        config.training_eval_split,
        knowledge_memory=knowledge_memory,
        knowledge_index_path=candidate.knowledge_index_path,
        calibrator_path=candidate.calibrator_path,
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
    report["artifact_status"] = "accepted" if quality_gate["passed"] else "rejected"
    _write_json_file(candidate.report_path, report)
    _write_json_file(
        candidate.manifest_path,
        build_training_manifest(
            generated_at=_now_iso(),
            include_samples=include_samples,
            sample_rows=sample_rows,
            training_tables=sorted(_configured_training_tables(config) or []),
            index_records=index_records,
            report=report,
            fingerprint=fingerprint,
            artifact_hashes={
                "knowledge_index_sha256": _hash_file(candidate.knowledge_index_path),
                "calibrator_sha256": _hash_file(candidate.calibrator_path),
                "training_report_sha256": _hash_file(candidate.report_path),
            },
        ),
    )
    if not quality_gate["passed"]:
        raise RuntimeError(
            "候选知识版本未通过 Dev 质量门禁，线上版本保持不变: "
            + "；".join(quality_gate["failures"])
        )

    registry.publish(candidate)
    _write_training_state(config, fingerprint, report)
    context_state.reset()

    logger.info("=== 知识版本 %s 已原子发布 ===", candidate.version)


async def _train_join_paths(live_schema, knowledge_memory, ctx, index_records, allowed_tables):
    logger.info("正在训练 join 路径...")
    for table_name, info in sorted(live_schema.items()):
        if not _should_include_table(table_name, allowed_tables):
            continue
        for fk in info.get("foreign_keys", []):
            referenced_table = str(fk.get("referenced_table") or "")
            if not referenced_table or not _should_include_table(referenced_table, allowed_tables):
                continue
            column_name = str(fk.get("column_name") or "")
            referenced_columns = set(
                live_schema.get(referenced_table, {}).get("columns", {}).keys()
            )
            referenced_column = column_name if column_name in referenced_columns else "主键"
            join_text = (
                "推荐关联路径:\n"
                f"事实表: {table_name}\n"
                f"关联字段: {column_name}\n"
                f"维度表: {referenced_table}\n"
                f"推荐写法: {table_name}.{column_name} = {referenced_table}.{referenced_column}"
            )
            await _save_training_text(
                knowledge_memory,
                join_text,
                ctx,
                index_records,
                source_type="join_path",
                table_names=[table_name, referenced_table],
                field_names=[column_name] if column_name else None,
                join_tables=[table_name, referenced_table],
                confidence=90,
            )


async def _train_feedback_examples(
    knowledge_memory,
    ctx,
    index_records,
    report,
    bundle: KnowledgeBundle,
    feedback_repository: SQLiteFeedbackRepository,
    current_schema_fingerprint: str,
    allowed_tables: set[str] | None,
):
    logger.info("正在加载运行期反馈正确样本...")
    raw_count = feedback_repository.count("gold")
    feedback_examples = feedback_repository.load_gold(
        current_schema_fingerprint=current_schema_fingerprint
    )
    report["feedback_examples_loaded"] = len(feedback_examples)
    report["feedback_examples_rejected"] = max(raw_count - len(feedback_examples), 0)
    if not feedback_examples:
        return
    for item in feedback_examples:
        question = str(item.get("question") or "").strip()
        sql = str(item.get("sql") or "").strip()
        if not question or not sql:
            continue
        metadata = _question_sql_metadata(question, sql, bundle.semantic_catalog(), allowed_tables)
        await _save_training_text(
            knowledge_memory,
            f"运行期反馈正确样本:\n问题: {question}\nSQL:\n{sql}",
            ctx,
            index_records,
            source_type="feedback_example",
            table_names=metadata["sql_tables"],
            aliases=_question_aliases(question),
            metric_tags=_metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=_time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            join_tables=metadata["sql_tables"],
            confidence=98,
        )
        report["feedback_examples"] += 1


async def _train_structured_knowledge(
    knowledge_memory,
    ctx,
    bundle: KnowledgeBundle,
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    allowed_tables: set[str] | None,
) -> None:
    """Train only typed, schema-validated knowledge records."""
    report["knowledge_source"] = "structured"
    report["structured_files"] = len(bundle.files)
    report["structured_records_rejected"] = len(bundle.errors)
    report["warnings"].extend(bundle.errors)
    trained = 0

    for card in bundle.table_cards:
        table = str(card["table"])
        text = (
            f"表卡片: {table}\n业务含义: {card.get('description', '')}\n"
            f"业务别名: {'、'.join(card.get('business_aliases', []))}\n"
            f"指标: {'、'.join(card.get('metrics', []))}\n"
            f"维度: {'、'.join(card.get('dimensions', []))}\n"
            f"关键字段: {'、'.join(card.get('important_columns', []))}"
        )
        await _save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="table_card",
            table_names=[table],
            field_names=card.get("important_columns", []),
            aliases=card.get("business_aliases", []),
            metric_tags=card.get("metrics", []),
            dimension_tags=card.get("dimensions", []),
            confidence=95,
        )
        trained += 1

    for metric in bundle.metrics:
        table = str(metric["source_table"])
        column = str(metric.get("column") or metric.get("expression"))
        text = (
            f"指标定义: {metric.get('name')}\n别名: {'、'.join(metric.get('aliases', []))}\n"
            f"计算: {metric.get('aggregation')}({table}.{column})\n"
            f"单位: {metric.get('unit', '')}"
        )
        await _save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="metric_rule",
            table_names=[table],
            field_names=[column],
            aliases=metric.get("aliases", []),
            metric_tags=[str(metric.get("id") or metric.get("name"))],
            confidence=98,
        )
        trained += 1

    for dimension in bundle.dimensions:
        table = str(dimension.get("table") or "")
        text = (
            f"维度定义: {dimension.get('name')}\n"
            f"别名: {'、'.join(dimension.get('aliases', []))}\n"
            f"来源: {table}.{','.join(dimension.get('columns', []))}"
        )
        await _save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="dimension_rule",
            table_names=[table],
            field_names=dimension.get("columns", []),
            aliases=dimension.get("aliases", []),
            dimension_tags=[str(dimension.get("id") or dimension.get("name"))],
            confidence=96,
        )
        trained += 1

    for join in bundle.joins:
        tables = [str(join["left_table"]), str(join["right_table"])]
        text = (
            f"业务关联: {join['left_table']}.{join['left_column']} = "
            f"{join['right_table']}.{join['right_column']}\n"
            f"用途: {join.get('purpose', '')}"
        )
        await _save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="join_path",
            table_names=tables,
            join_tables=tables,
            field_names=[str(join["left_column"]), str(join["right_column"])],
            confidence=99,
        )
        trained += 1

    for policy in bundle.policies:
        text = "业务策略: " + json.dumps(policy, ensure_ascii=False, sort_keys=True)
        await _save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="domain_policy",
            table_names=[str(policy["table"])] if policy.get("table") else None,
            field_names=[str(policy["column"])] if policy.get("column") else None,
            aliases=policy.get("terms", []),
            confidence=98,
        )
        trained += 1

    pairs = [(str(item["question"]), str(item["sql"])) for item in bundle.gold_sql]
    await _train_question_sql_examples(
        knowledge_memory,
        ctx,
        pairs,
        index_records,
        report,
        bundle.semantic_catalog(),
        allowed_tables,
    )
    trained += len(pairs)
    for refusal in bundle.refusals:
        await _save_training_text(
            knowledge_memory,
            f"拒答示例\n问题: {refusal.get('question')}\n原因: {refusal.get('reason')}",
            ctx,
            index_records,
            source_type="refusal_example",
            confidence=98,
        )
        trained += 1
    report["question_sql_examples"] = len(pairs)
    report["structured_records"] = trained
