"""Read and cache authoritative SQL Server schema metadata."""

import time
from typing import Any

from ..core.exceptions import DatabaseConnectionError
from ..core.logging import setup_logging
from .context_state import ContextRuntimeState
from .ports import SqlExecutor

logger = setup_logging("text2sql.schema")

LIVE_SCHEMA_QUERY = """
SELECT
    T.NAME AS TABLE_NAME,
    C.NAME AS COLUMN_NAME,
    TYP.NAME AS DATA_TYPE,
    C.IS_NULLABLE AS IS_NULLABLE,
    CAST(EP.VALUE AS NVARCHAR(4000)) AS COLUMN_DESCRIPTION
FROM SYS.TABLES T
INNER JOIN SYS.COLUMNS C ON T.OBJECT_ID = C.OBJECT_ID
INNER JOIN SYS.TYPES TYP ON C.USER_TYPE_ID = TYP.USER_TYPE_ID
LEFT JOIN SYS.EXTENDED_PROPERTIES EP
    ON T.OBJECT_ID = EP.MAJOR_ID
    AND C.COLUMN_ID = EP.MINOR_ID
    AND EP.NAME = 'MS_DESCRIPTION'
ORDER BY T.NAME, C.COLUMN_ID
"""

LIVE_TABLE_QUERY = """
SELECT
    T.NAME AS TABLE_NAME,
    CAST(P.VALUE AS NVARCHAR(4000)) AS TABLE_DESCRIPTION
FROM SYS.TABLES T
LEFT JOIN SYS.EXTENDED_PROPERTIES P
    ON T.OBJECT_ID = P.MAJOR_ID
    AND P.MINOR_ID = 0
    AND P.NAME = 'MS_DESCRIPTION'
ORDER BY T.NAME
"""

LIVE_FK_QUERY = """
SELECT
    OBJECT_NAME(F.PARENT_OBJECT_ID) AS TABLE_NAME,
    COL_NAME(FC.PARENT_OBJECT_ID, FC.PARENT_COLUMN_ID) AS COLUMN_NAME,
    OBJECT_NAME(F.REFERENCED_OBJECT_ID) AS REFERENCED_TABLE
FROM SYS.FOREIGN_KEYS F
INNER JOIN SYS.FOREIGN_KEY_COLUMNS FC
    ON F.OBJECT_ID = FC.CONSTRAINT_OBJECT_ID
"""


def _schema_cache_valid(state: ContextRuntimeState, ttl_seconds: int) -> bool:
    if state.live_schema is None:
        return False
    if ttl_seconds <= 0:
        return True
    return (time.monotonic() - state.live_schema_cached_at) < ttl_seconds


async def _run_sql(sql: str, sql_executor: SqlExecutor) -> Any:
    return await sql_executor.execute(sql, timeout_seconds=30.0)


async def get_live_schema(
    cache_ttl_seconds: int,
    force_refresh: bool = False,
    *,
    sql_executor: SqlExecutor,
    state: ContextRuntimeState,
) -> dict[str, dict[str, Any]]:
    """Return authoritative metadata with TTL and single-flight refresh."""
    if not force_refresh and _schema_cache_valid(state, cache_ttl_seconds):
        assert state.live_schema is not None
        return state.live_schema

    async with state.live_schema_lock:
        if not force_refresh and _schema_cache_valid(state, cache_ttl_seconds):
            assert state.live_schema is not None
            return state.live_schema
        try:
            return await _load_live_schema(sql_executor, state)
        except DatabaseConnectionError:
            raise
        except Exception as exc:
            raise DatabaseConnectionError(
                "无法读取实时 SQL Server Schema；请检查 ODBC Driver、TLS/证书、"
                "服务器网络以及只读账号的元数据权限"
            ) from exc


async def _load_live_schema(
    sql_executor: SqlExecutor, state: ContextRuntimeState
) -> dict[str, dict[str, Any]]:
    schema: dict[str, dict[str, Any]] = {}

    table_df = await _run_sql(LIVE_TABLE_QUERY, sql_executor)
    for _, row in table_df.iterrows():
        table_name = str(row["TABLE_NAME"])
        schema[table_name] = {
            "description": str(row.get("TABLE_DESCRIPTION") or ""),
            "columns": {},
            "foreign_keys": [],
        }

    column_df = await _run_sql(LIVE_SCHEMA_QUERY, sql_executor)
    for _, row in column_df.iterrows():
        table_name = str(row["TABLE_NAME"])
        table_info = schema.setdefault(
            table_name, {"description": "", "columns": {}, "foreign_keys": []}
        )
        table_info["columns"][str(row["COLUMN_NAME"])] = {
            "data_type": str(row["DATA_TYPE"]),
            "is_nullable": bool(row["IS_NULLABLE"]),
            "description": str(row.get("COLUMN_DESCRIPTION") or ""),
        }

    fk_df = await _run_sql(LIVE_FK_QUERY, sql_executor)
    for _, row in fk_df.iterrows():
        table_name = str(row["TABLE_NAME"])
        table_info = schema.setdefault(
            table_name, {"description": "", "columns": {}, "foreign_keys": []}
        )
        table_info["foreign_keys"].append(
            {
                "column_name": str(row["COLUMN_NAME"]),
                "referenced_table": str(row["REFERENCED_TABLE"]),
            }
        )

    state.live_schema = schema
    state.live_schema_cached_at = time.monotonic()
    logger.info("Loaded live schema for %d tables", len(schema))
    return schema
