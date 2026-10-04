"""Explicit dependency container for one application process."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from functools import cached_property
from typing import cast

from ..application.context_state import ContextRuntimeState
from ..application.text2sql_service import Text2SQLService
from ..core.config import Settings
from ..infrastructure.context_adapters import SnapshotArtifactProvider, SqlServerSchemaRepository
from ..infrastructure.feedback_repository import SQLiteFeedbackRepository
from ..infrastructure.query_adapters import VannaSqlExecutor
from ..infrastructure.runtime import RuntimeResources
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.snapshot import ArtifactSnapshot
from .wiring import build_text2sql_service, feedback_policy


class ApplicationContainer:
    def __init__(self, config: Settings):
        self.config = config
        self.runtime = RuntimeResources(config)
        self.context_state = ContextRuntimeState()
        self._resource_lock = threading.RLock()
        self._closed = False

    def _resource[T](self, name: str, factory: Callable[[], T]) -> T:
        """Publish one lazy resource atomically when getters run in workers."""
        with self._resource_lock:
            if self._closed:
                raise RuntimeError("Application container is closed")
            if name in self.__dict__:
                return cast(T, self.__dict__[name])
            resource = factory()
            self.__dict__[name] = resource
            return resource

    @cached_property
    def feedback_repository(self) -> SQLiteFeedbackRepository:
        config = self.config
        return self._resource(
            "feedback_repository",
            lambda: SQLiteFeedbackRepository(config.feedback_db_path, feedback_policy(config)),
        )

    @cached_property
    def sql_executor(self) -> VannaSqlExecutor:
        return self._resource(
            "sql_executor",
            lambda: VannaSqlExecutor(
                self.runtime.sql_runner, max_concurrency=self.config.sql_max_concurrency
            ),
        )

    @cached_property
    def schema_repository(self) -> SqlServerSchemaRepository:
        return self._resource(
            "schema_repository",
            lambda: SqlServerSchemaRepository(
                self.sql_executor, self.context_state, self.config.schema_cache_ttl_seconds
            ),
        )

    @cached_property
    def artifact_provider(self) -> SnapshotArtifactProvider:
        return self._resource("artifact_provider", self._create_artifact_provider)

    def _create_artifact_provider(self) -> SnapshotArtifactProvider:
        active = KnowledgeArtifactRegistry(
            self.config.knowledge_artifact_dir,
            self.config.knowledge_active_pointer_path,
        ).load_active()
        if active is None:
            raise RuntimeError("no valid active knowledge artifact")
        return SnapshotArtifactProvider(
            ArtifactSnapshot.load(
                active.snapshot_path, require_reviewed=self.config.app_env == "production"
            )
        )

    @cached_property
    def query_service(self) -> Text2SQLService:
        return self._resource(
            "query_service",
            lambda: build_text2sql_service(
                self.config,
                self.runtime,
                feedback_repository=self.feedback_repository,
                sql_executor=self.sql_executor,
                context_state=self.context_state,
            ),
        )

    def close(self) -> None:
        with self._resource_lock:
            self._closed = True
        failures: list[Exception] = []
        for name, cleanup in (
            ("context state reset", self.context_state.reset),
            ("runtime close", self.runtime.close),
        ):
            try:
                cleanup()
            except Exception as exc:
                exc.add_note(f"while running {name}")
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Container synchronous cleanup failed", failures)

    async def aclose(self) -> None:
        """Release all initialized resources; never initialize lazy ones to close."""
        failures: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        with self._resource_lock:
            self._closed = True
            service = self.__dict__.get("query_service")
            executor = self.__dict__.get("sql_executor")
        for resource in (service, executor):
            if resource is not None:
                try:
                    await resource.aclose()
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except Exception as exc:
                    exc.add_note(f"while closing {type(resource).__name__}")
                    failures.append(exc)
        try:
            self.close()
        except Exception as exc:
            failures.append(exc)
        if cancelled is not None:
            if failures:
                raise cancelled from ExceptionGroup("Container shutdown failed", failures)
            raise cancelled
        if failures:
            raise ExceptionGroup("Container shutdown failed", failures)
