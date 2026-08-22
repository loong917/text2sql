"""Infrastructure adapters for the online Text2SQL application ports."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

import pandas as pd
from vanna.capabilities.sql_runner.models import RunSqlToolArgs


class CallableRetriever:
    def __init__(self, function: Callable[[str], Awaitable[dict[str, Any]]]):
        self._function = function

    async def retrieve(self, question: str) -> dict[str, Any]:
        return await self._function(question)


class CallableValidator:
    def __init__(self, function: Callable[..., str | None]):
        self._function = function

    def validate(
        self,
        sql: str,
        live_schema: dict[str, dict[str, Any]],
        **context: Any,
    ) -> str | None:
        return self._function(sql, live_schema, **context)


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

    def _execute_sync(self, sql: str, timeout_seconds: float) -> Any:
        engine = getattr(self._runner, "engine", None)
        sa = getattr(self._runner, "sa", None)
        if engine is not None and sa is not None:
            with engine.connect() as connection:
                driver_connection = getattr(connection.connection, "driver_connection", None)
                previous_timeout = getattr(driver_connection, "timeout", None)
                if driver_connection is not None:
                    driver_connection.timeout = max(1, math.ceil(timeout_seconds))
                try:
                    return pd.read_sql_query(sa.text(sql), connection)
                finally:
                    if driver_connection is not None and previous_timeout is not None:
                        driver_connection.timeout = previous_timeout

        # Test doubles and non-MSSQL Vanna runners still go through a worker
        # thread, so even an incorrectly implemented async adapter cannot block
        # the web server's event loop.
        return asyncio.run(self._runner.run_sql(RunSqlToolArgs(sql=sql), None))

    async def execute(self, sql: str, *, timeout_seconds: float) -> Any:
        async with self._semaphore:
            return await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, sql, timeout_seconds),
                timeout=timeout_seconds + 1.0,
            )
