"""Explicit dependency container for one application process."""

from __future__ import annotations

from functools import cached_property

from ..application.context_state import ContextRuntimeState
from ..application.text2sql_service import Text2SQLService
from ..core.config import Settings
from ..infrastructure.feedback_repository import FeedbackPolicy, SQLiteFeedbackRepository
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from .wiring import build_text2sql_service


class ApplicationContainer:
    def __init__(self, config: Settings):
        self.config = config
        self.runtime = RuntimeResources(config)
        self.context_state = ContextRuntimeState()

    @cached_property
    def feedback_repository(self) -> SQLiteFeedbackRepository:
        config = self.config
        return SQLiteFeedbackRepository(
            config.feedback_db_path,
            FeedbackPolicy(
                enabled=config.enable_feedback_capture,
                require_execution_success=config.feedback_require_execution_success,
                require_nonempty_result=config.feedback_require_nonempty_result,
                min_result_rows=config.feedback_min_result_rows,
                min_quality_score=config.feedback_min_quality_score,
            ),
        )

    @cached_property
    def sql_executor(self) -> VannaSqlExecutor:
        return VannaSqlExecutor(
            self.runtime.sql_runner,
            max_concurrency=self.config.sql_max_concurrency,
        )

    @cached_property
    def query_service(self) -> Text2SQLService:
        return build_text2sql_service(
            self.config,
            self.runtime,
            feedback_repository=self.feedback_repository,
            sql_executor=self.sql_executor,
            context_state=self.context_state,
        )

    def close(self) -> None:
        self.context_state.reset()
        self.runtime.close()
