"""Composition root connecting application protocols to infrastructure adapters."""

from __future__ import annotations

from functools import partial
from typing import Any

from ..application.constants import REFUSAL_TOKEN
from ..application.context_service import ContextServiceConfig, build_prompt_context, validate_sql
from ..application.context_state import ContextRuntimeState
from ..application.ports import FeedbackRepository, SqlExecutor
from ..application.query_config import QueryServiceConfig
from ..application.sql_policy import SqlValidationConfig
from ..application.text2sql_service import Text2SQLDependencies, Text2SQLService
from ..core.config import Settings
from ..infrastructure.feedback_repository import FeedbackPolicy, SQLiteFeedbackRepository
from ..infrastructure.ollama_generator import OllamaSqlGenerator
from ..infrastructure.query_adapters import (
    CallableRetriever,
    CallableValidator,
    VannaSqlExecutor,
)
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..retrieval.dataset import retrieval_dataset_fingerprint


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
    context_state = context_state or ContextRuntimeState()
    feedback_repository = feedback_repository or SQLiteFeedbackRepository(
        config.feedback_db_path,
        FeedbackPolicy(
            enabled=config.enable_feedback_capture,
            require_execution_success=config.feedback_require_execution_success,
            require_nonempty_result=config.feedback_require_nonempty_result,
            min_result_rows=config.feedback_min_result_rows,
            min_quality_score=config.feedback_min_quality_score,
        ),
    )
    if knowledge_index_path is None:
        active = KnowledgeArtifactRegistry(
            config.knowledge_artifact_dir,
            config.knowledge_active_pointer_path,
        ).load_active()
        if active is None:
            raise RuntimeError(
                "no complete active knowledge artifact; run text2sql-train before serving queries"
            )
        knowledge_index_path = active.knowledge_index_path
        calibrator_path = calibrator_path or active.calibrator_path
    elif calibrator_path is None:
        calibrator_path = config.table_retrieval_calibrator_path
    context_config = ContextServiceConfig(
        knowledge_index_path=knowledge_index_path,
        structured_knowledge_dir=config.structured_knowledge_dir,
        schema_cache_ttl_seconds=config.schema_cache_ttl_seconds,
        prompt_feedback_examples=config.prompt_feedback_examples,
        llm_host=config.llm_host,
        llm_timeout_seconds=config.llm_timeout_seconds,
        llm_keep_alive=config.llm_keep_alive,
        embedding_model=config.embedding_model,
        table_retrieval_calibrator_path=calibrator_path,
        table_retrieval_token_budget=config.table_retrieval_token_budget,
        table_retrieval_require_calibration=config.table_retrieval_require_calibration,
        table_retrieval_dataset_fingerprint=retrieval_dataset_fingerprint(
            config.retrieval_train_set_path,
            config.retrieval_calibration_set_path,
            config.retrieval_test_set_path,
        ),
    )
    sql_validation_config = SqlValidationConfig(
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
    sql_executor = sql_executor or VannaSqlExecutor(
        runtime.sql_runner, max_concurrency=config.sql_max_concurrency
    )
    dependencies = Text2SQLDependencies(
        retriever=CallableRetriever(
            partial(
                build_prompt_context,
                config=context_config,
                feedback_repository=feedback_repository,
                sql_executor=sql_executor,
                knowledge_memory=knowledge_memory or runtime.knowledge_memory,
                state=context_state,
            )
        ),
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
        validator=CallableValidator(partial(validate_sql, config=sql_validation_config)),
        executor=sql_executor,
        repository=feedback_repository,
    )
    return Text2SQLService(
        dependencies,
        QueryServiceConfig(
            max_result_rows=config.max_result_rows,
            query_timeout_seconds=config.sql_query_timeout_seconds,
            feedback_min_result_rows=config.feedback_min_result_rows,
        ),
    )
