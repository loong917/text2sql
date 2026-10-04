"""Composition root for evaluation-only dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..application.context_state import ContextRuntimeState
from ..bootstrap.wiring import build_text2sql_service, validation_config
from ..core.config import Settings
from ..domain.sql_validation import SqlSafetyPolicy
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..infrastructure.schema_repository import get_live_schema
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.provenance import schema_fingerprint
from ..knowledge.snapshot import ArtifactSnapshot
from .service import EvaluationConfig, EvaluationService


async def build_evaluation_service(
    config: Settings,
    runtime: RuntimeResources,
    *,
    knowledge_memory: Any | None = None,
    knowledge_index_path: str | None = None,
    calibrator_path: str | None = None,
    context_state: ContextRuntimeState | None = None,
) -> EvaluationService:
    context_state = context_state or ContextRuntimeState()
    sql_executor = VannaSqlExecutor(runtime.sql_runner, max_concurrency=config.sql_max_concurrency)
    try:
        schema = await get_live_schema(
            config.schema_cache_ttl_seconds,
            sql_executor=sql_executor,
            state=context_state,
        )
        if knowledge_index_path is None:
            active = KnowledgeArtifactRegistry(
                config.knowledge_artifact_dir, config.knowledge_active_pointer_path
            ).load_active()
            if active is None:
                raise RuntimeError("evaluation requires a complete active knowledge artifact")
            knowledge_index_path = active.knowledge_index_path
            calibrator_path = active.calibrator_path
            knowledge_memory = knowledge_memory or runtime.create_knowledge_memory(
                collection_name=active.collection_name
            )
        snapshot = ArtifactSnapshot.load(
            Path(knowledge_index_path).parent / "knowledge_snapshot.json",
            require_reviewed=config.app_env == "production",
        )
        if schema_fingerprint(schema) != schema_fingerprint(snapshot.schema):
            raise RuntimeError("live database schema differs from the evaluated knowledge snapshot")
        catalog = snapshot.knowledge.semantic_catalog()
        sql_policy = validation_config(config)
        safety_policy = SqlSafetyPolicy(
            allowed_schemas=tuple(
                item.strip() for item in sql_policy.allowed_schemas.split(",") if item.strip()
            ),
            max_joins=sql_policy.max_joins,
            max_subqueries=sql_policy.max_subqueries,
            allowed_tables=tuple(
                item.strip() for item in sql_policy.allowed_tables.split(",") if item.strip()
            ),
            denied_tables=tuple(
                item.strip() for item in sql_policy.denied_tables.split(",") if item.strip()
            ),
            denied_columns=tuple(
                item.strip() for item in sql_policy.denied_columns.split(",") if item.strip()
            ),
            aggregation_only_tables=tuple(
                item.strip()
                for item in sql_policy.aggregation_only_tables.split(",")
                if item.strip()
            ),
            allow_select_star=sql_policy.allow_select_star,
            require_table=sql_policy.require_table,
            allow_cross_join=sql_policy.allow_cross_join,
        )
        return EvaluationService(
            query_service=build_text2sql_service(
                config,
                runtime,
                sql_executor=sql_executor,
                knowledge_memory=knowledge_memory,
                knowledge_index_path=knowledge_index_path,
                calibrator_path=calibrator_path,
                context_state=context_state,
            ),
            sql_executor=sql_executor,
            catalog=catalog,
            config=EvaluationConfig(
                dev_set_path=config.eval_dev_set_path,
                test_set_path=config.eval_test_set_path,
                query_timeout_seconds=config.sql_query_timeout_seconds,
                max_result_rows=config.max_result_rows,
                live_schema=schema,
                safety_policy=safety_policy,
            ),
        )
    except BaseException:
        await sql_executor.aclose()
        raise


async def run_configured_evaluation(
    config: Settings,
    runtime: RuntimeResources,
    split: str = "dev",
    *,
    dataset_bytes: bytes | None = None,
    knowledge_memory: Any | None = None,
    knowledge_index_path: str | None = None,
    calibrator_path: str | None = None,
) -> list[dict]:
    service = await build_evaluation_service(
        config,
        runtime,
        knowledge_memory=knowledge_memory,
        knowledge_index_path=knowledge_index_path,
        calibrator_path=calibrator_path,
    )
    try:
        return await service.run(split, dataset_bytes=dataset_bytes)
    finally:
        await service.aclose()
