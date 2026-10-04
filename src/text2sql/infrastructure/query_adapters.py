"""Infrastructure adapters for the online Text2SQL application ports."""

from __future__ import annotations

import asyncio
import math
import threading
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd
from vanna.capabilities.sql_runner.models import RunSqlToolArgs

from ..application.contracts import QueryContext, ValidationResult


class CallableRetriever:
    def __init__(
        self, function: Callable[[str], Awaitable[QueryContext]], *, resources: tuple[Any, ...] = ()
    ):
        self._function = function
        self._resources = resources

    async def retrieve(self, question: str) -> QueryContext:
        return await self._function(question)

    async def aclose(self) -> None:
        """Close every owned resource even when an earlier close fails."""
        failures: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        for resource in self._resources:
            try:
                await resource.aclose()
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception as exc:
                exc.add_note(f"while closing {type(resource).__name__}")
                failures.append(exc)
        if cancelled is not None:
            if failures:
                raise cancelled from ExceptionGroup("Retriever shutdown failed", failures)
            raise cancelled
        if failures:
            raise ExceptionGroup("Retriever shutdown failed", failures)


class CallableValidator:
    def __init__(self, function: Callable[..., str | None]):
        self._function = function

    def validate(
        self,
        sql: str,
        live_schema: dict[str, dict[str, Any]],
        **context: Any,
    ) -> ValidationResult:
        error = self._function(sql, live_schema, **context)
        return ValidationResult(not error, "SQL_VALIDATION_FAILED" if error else None, error)


class VannaSqlExecutor:
    """Run Vanna's synchronous MSSQL implementation outside the event loop.

    ``MSSQLRunner.run_sql`` is declared async but currently performs blocking
    SQLAlchemy/Pandas work without yielding.  Running that coroutine directly
    would freeze every request on the FastAPI event loop and make
    ``asyncio.wait_for`` ineffective.  The adapter therefore owns the blocking
    boundary and also applies a DBAPI statement timeout when the runner exposes
    its SQLAlchemy engine.
    """

    def __init__(self, runner: Any, *, max_concurrency: int = 4):
        self._runner = runner
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, max_concurrency), thread_name_prefix="sql-worker"
        )
        self._jobs: set[asyncio.Future[Any]] = set()
        self._cursors: dict[str, Any] = {}
        self._cursor_lock = threading.Lock()
        self._local = threading.local()
        self._closed = False

    def _execute_sync(self, sql: str, timeout_seconds: float) -> Any:
        engine = getattr(self._runner, "engine", None)
        sa = getattr(self._runner, "sa", None)
        if engine is not None and sa is not None:
            with engine.connect() as connection:
                driver_connection = getattr(connection.connection, "driver_connection", None)
                if driver_connection is None:
                    raise RuntimeError("SQL connection exposes no cancellable DBAPI connection")
                previous_timeout = getattr(driver_connection, "timeout", None)
                if driver_connection is not None:
                    driver_connection.timeout = max(1, math.ceil(timeout_seconds))
                try:
                    cursor = driver_connection.cursor()
                    with self._cursor_lock:
                        self._cursors[self._local.job_id] = cursor
                    try:
                        cursor.execute(sql)
                        columns = [item[0] for item in cursor.description or []]
                        return pd.DataFrame.from_records(
                            [tuple(row) for row in cursor.fetchall()], columns=columns
                        )
                    finally:
                        with self._cursor_lock:
                            self._cursors.pop(self._local.job_id, None)
                        cursor.close()
                finally:
                    if driver_connection is not None and previous_timeout is not None:
                        driver_connection.timeout = previous_timeout

        # Test doubles and non-MSSQL Vanna runners still go through a worker
        # thread, so even an incorrectly implemented async adapter cannot block
        # the web server's event loop.
        return asyncio.run(self._runner.run_sql(RunSqlToolArgs(sql=sql), None))

    async def execute(self, sql: str, *, timeout_seconds: float) -> Any:
        if self._closed:
            raise RuntimeError("SQL executor is closed")
        await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_seconds + 1.0)
        if self._closed:
            self._semaphore.release()
            raise RuntimeError("SQL executor is closed")
        job_id = uuid.uuid4().hex

        def work() -> Any:
            self._local.job_id = job_id
            return self._execute_sync(sql, timeout_seconds)

        future = asyncio.get_running_loop().run_in_executor(self._pool, work)
        self._jobs.add(future)

        def finished(job: asyncio.Future[Any]) -> None:
            self._jobs.discard(job)
            self._semaphore.release()
            if not job.cancelled():
                job.exception()  # consume errors after caller timeout/cancellation

        future.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds + 1.0)
        except (TimeoutError, asyncio.CancelledError):
            with self._cursor_lock:
                cursor = self._cursors.get(job_id)
            if cursor is not None:

                async def cancel_cursor() -> None:
                    try:
                        await asyncio.to_thread(cursor.cancel)
                    except Exception:
                        pass  # worker retains its permit until it actually exits

                asyncio.create_task(cancel_cursor())
            raise

    async def aclose(self, drain_timeout: float = 5.0) -> None:
        self._closed = True
        try:
            if self._jobs:
                await asyncio.wait(self._jobs, timeout=drain_timeout)
        finally:
            # Cancellation of the drain must still reject queued worker jobs.
            self._pool.shutdown(wait=False, cancel_futures=True)
