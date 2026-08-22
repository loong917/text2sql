"""Composition root for evaluation-only dependencies."""

from __future__ import annotations

from typing import Any

from ..application.context_state import ContextRuntimeState
from ..application.schema_repository import get_live_schema
from ..bootstrap.wiring import build_text2sql_service
from ..core.config import Settings
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.structured import load_validated_knowledge_bundle
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
    schema = await get_live_schema(
        config.schema_cache_ttl_seconds,
        sql_executor=sql_executor,
        state=context_state,
    )
    catalog = load_validated_knowledge_bundle(
        config.structured_knowledge_dir, schema
    ).semantic_catalog()
    return EvaluationService(
        query_service=build_text2sql_service(
            config,
            runtime,
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
        ),
    )


async def run_configured_evaluation(
    config: Settings,
    runtime: RuntimeResources,
    split: str = "dev",
    *,
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
    return await service.run(split)
