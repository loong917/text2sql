"""Composition root; only adapters know model, storage and database implementations."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from ..application.constants import REFUSAL_TOKEN
from ..application.context_service import ContextServiceConfig, build_prompt_context, validate_sql
from ..application.context_state import ContextRuntimeState
from ..application.ports import FeedbackRepository, SqlExecutor
from ..application.query_config import QueryServiceConfig
from ..application.sql_policy import SqlValidationConfig
from ..application.text2sql_service import Text2SQLDependencies, Text2SQLService
from ..core.config import Settings
from ..domain.sql_compiler import SQLCompiler
from ..infrastructure.context_adapters import (
    CatalogSemanticParser,
    SnapshotArtifactProvider,
    SqlServerSchemaRepository,
    VannaKnowledgeMemory,
)
from ..infrastructure.feedback_repository import FeedbackPolicy, SQLiteFeedbackRepository
from ..infrastructure.ollama_generator import OllamaSqlGenerator
from ..infrastructure.query_adapters import CallableRetriever, CallableValidator, VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.snapshot import ArtifactSnapshot
from ..retrieval.table_retriever import OllamaEmbedder, TableRetriever


def feedback_policy(config: Settings) -> FeedbackPolicy:
    return FeedbackPolicy(
        enabled=config.enable_feedback_capture,
        require_execution_success=config.feedback_require_execution_success,
        require_nonempty_result=config.feedback_require_nonempty_result,
        min_result_rows=config.feedback_min_result_rows,
        min_quality_score=config.feedback_min_quality_score,
    )


def validation_config(config: Settings) -> SqlValidationConfig:
    return SqlValidationConfig(
        allowed_schemas=config.sql_allowed_schemas,
        max_joins=config.sql_max_joins,
        max_subqueries=config.sql_max_subqueries,
        allowed_tables=config.sql_allowed_tables,
        denied_tables=config.sql_denied_tables,
        denied_columns=config.sql_denied_columns,
        aggregation_only_tables=config.sql_aggregation_only_tables,
        allow_select_star=config.sql_allow_select_star,
        require_table=config.sql_require_table,
        allow_cross_join=config.sql_allow_cross_join,
    )


def build_text2sql_service(
    config: Settings,
    runtime: RuntimeResources,
    *,
    feedback_repository: FeedbackRepository | None = None,
    sql_executor: SqlExecutor | None = None,
    knowledge_memory: Any | None = None,
    knowledge_index_path: str | None = None,
    calibrator_path: str | None = None,
    context_state: ContextRuntimeState | None = None,
) -> Text2SQLService:
    if knowledge_index_path is not None and knowledge_memory is None:
        raise ValueError("an explicit knowledge index requires its matching versioned memory")
    state = context_state or ContextRuntimeState()
    repository = feedback_repository or SQLiteFeedbackRepository(
        config.feedback_db_path, feedback_policy(config)
    )
    if knowledge_index_path is None:
        active = KnowledgeArtifactRegistry(
            config.knowledge_artifact_dir, config.knowledge_active_pointer_path
        ).load_active()
        if active is None:
            raise RuntimeError(
                "no complete active artifact; rebuild and evaluate before serving queries"
            )
        knowledge_index_path = active.knowledge_index_path
        calibrator_path = active.calibrator_path
        if knowledge_memory is None:
            knowledge_memory = runtime.create_knowledge_memory(
                collection_name=active.collection_name
            )
    snapshot = ArtifactSnapshot.load(
        Path(knowledge_index_path).parent / "knowledge_snapshot.json",
        require_reviewed=config.app_env == "production",
    )
    provider = SnapshotArtifactProvider(snapshot)
    executor = sql_executor or VannaSqlExecutor(
        runtime.sql_runner, max_concurrency=config.sql_max_concurrency
    )
    table_selector = TableRetriever(
        embedder=OllamaEmbedder(
            host=config.llm_host,
            timeout_seconds=config.llm_timeout_seconds,
            model=config.embedding_model,
            keep_alive=config.llm_keep_alive,
        ),
        calibrator_path=calibrator_path or config.table_retrieval_calibrator_path,
        token_budget=config.table_retrieval_token_budget,
        embedding_model=config.embedding_model,
        embedding_model_digest=config.embedding_model_digest or None,
        require_calibration=config.table_retrieval_require_calibration,
        dataset_fingerprint=snapshot.retrieval_dataset_fingerprint,
        business_cards=tuple(snapshot.knowledge.table_cards),
    )
    context_config = ContextServiceConfig(
        knowledge_index_path=knowledge_index_path,
        prompt_feedback_examples=config.prompt_feedback_examples,
        prompt_token_budget=max(256, config.llm_num_ctx - config.llm_num_predict - 768),
        use_live_feedback=config.app_env != "production",
    )
    retriever = CallableRetriever(
        partial(
            build_prompt_context,
            config=context_config,
            feedback_repository=repository,
            schema_repository=SqlServerSchemaRepository(
                executor, state, config.schema_cache_ttl_seconds
            ),
            knowledge_memory=VannaKnowledgeMemory(knowledge_memory),
            artifact_provider=provider,
            semantic_parser=CatalogSemanticParser(),
            table_selector=table_selector,
            state=state,
        ),
        resources=(table_selector,) if sql_executor is not None else (table_selector, executor),
    )
    return Text2SQLService(
        Text2SQLDependencies(
            retriever=retriever,
            generator=OllamaSqlGenerator(
                host=config.llm_host,
                timeout_seconds=config.llm_timeout_seconds,
                model=config.llm_model,
                max_concurrency=config.llm_max_concurrency,
                num_ctx=config.llm_num_ctx,
                num_predict=config.llm_num_predict,
                keep_alive=config.llm_keep_alive,
                refusal_token=REFUSAL_TOKEN,
            ),
            validator=CallableValidator(partial(validate_sql, config=validation_config(config))),
            executor=executor,
            repository=repository,
            compiler=SQLCompiler(),
        ),
        QueryServiceConfig(
            config.max_result_rows,
            config.sql_query_timeout_seconds,
            config.feedback_min_result_rows,
        ),
    )
