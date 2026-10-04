"""Read and cache authoritative SQL Server schema metadata."""

import time
from math import isnan
from numbers import Integral
from typing import Any

from ..application.context_state import ContextRuntimeState
from ..application.ports import SqlExecutor
from ..core.exceptions import DatabaseConnectionError
from ..core.logging import setup_logging
from ..knowledge.schema_contract import require_schema_contract

logger = setup_logging("text2sql.schema")

LIVE_SCHEMA_QUERY = """
SELECT
    T.NAME AS TABLE_NAME,
    SCHEMA_NAME(T.SCHEMA_ID) AS SCHEMA_NAME,
    C.NAME AS COLUMN_NAME,
    TYP.NAME AS DATA_TYPE,
    C.IS_NULLABLE AS IS_NULLABLE,
    C.MAX_LENGTH AS MAX_LENGTH,
    C.PRECISION AS NUMERIC_PRECISION,
    C.SCALE AS NUMERIC_SCALE,
    C.COLLATION_NAME AS COLLATION_NAME,
    C.IS_IDENTITY AS IS_IDENTITY,
    C.IS_COMPUTED AS IS_COMPUTED,
    CAST(EP.VALUE AS NVARCHAR(4000)) AS COLUMN_DESCRIPTION
FROM SYS.TABLES T
INNER JOIN SYS.COLUMNS C ON T.OBJECT_ID = C.OBJECT_ID
INNER JOIN SYS.TYPES TYP ON C.USER_TYPE_ID = TYP.USER_TYPE_ID
LEFT JOIN SYS.EXTENDED_PROPERTIES EP
    ON T.OBJECT_ID = EP.MAJOR_ID
    AND C.COLUMN_ID = EP.MINOR_ID
    AND EP.NAME = 'MS_DESCRIPTION'
    AND EP.CLASS = 1
ORDER BY T.SCHEMA_ID, T.NAME, C.COLUMN_ID
"""

LIVE_TABLE_QUERY = """
SELECT
    T.NAME AS TABLE_NAME,
    SCHEMA_NAME(T.SCHEMA_ID) AS SCHEMA_NAME,
    T.OBJECT_ID AS OBJECT_ID,
    CAST(P.VALUE AS NVARCHAR(4000)) AS TABLE_DESCRIPTION
FROM SYS.TABLES T
LEFT JOIN SYS.EXTENDED_PROPERTIES P
    ON T.OBJECT_ID = P.MAJOR_ID
    AND P.MINOR_ID = 0
    AND P.NAME = 'MS_DESCRIPTION'
    AND P.CLASS = 1
ORDER BY T.SCHEMA_ID, T.NAME
"""

LIVE_FK_QUERY = """
SELECT
    OBJECT_NAME(F.PARENT_OBJECT_ID) AS TABLE_NAME,
    OBJECT_SCHEMA_NAME(F.PARENT_OBJECT_ID) AS SCHEMA_NAME,
    COL_NAME(FC.PARENT_OBJECT_ID, FC.PARENT_COLUMN_ID) AS COLUMN_NAME,
    OBJECT_NAME(F.REFERENCED_OBJECT_ID) AS REFERENCED_TABLE,
    OBJECT_SCHEMA_NAME(F.REFERENCED_OBJECT_ID) AS REFERENCED_SCHEMA,
    COL_NAME(FC.REFERENCED_OBJECT_ID, FC.REFERENCED_COLUMN_ID) AS REFERENCED_COLUMN,
    F.NAME AS CONSTRAINT_NAME,
    FC.CONSTRAINT_COLUMN_ID AS COLUMN_ORDINAL,
    F.IS_DISABLED AS IS_DISABLED,
    F.IS_NOT_TRUSTED AS IS_NOT_TRUSTED
FROM SYS.FOREIGN_KEYS F
INNER JOIN SYS.FOREIGN_KEY_COLUMNS FC
    ON F.OBJECT_ID = FC.CONSTRAINT_OBJECT_ID
ORDER BY F.PARENT_OBJECT_ID, F.NAME, FC.CONSTRAINT_COLUMN_ID
"""

LIVE_KEY_QUERY = """
SELECT T.NAME AS TABLE_NAME, SCHEMA_NAME(T.SCHEMA_ID) AS SCHEMA_NAME,
    I.NAME AS CONSTRAINT_NAME, I.IS_PRIMARY_KEY AS IS_PRIMARY_KEY,
    C.NAME AS COLUMN_NAME, IC.KEY_ORDINAL AS COLUMN_ORDINAL
FROM SYS.TABLES T
JOIN SYS.INDEXES I ON T.OBJECT_ID = I.OBJECT_ID AND I.IS_UNIQUE = 1
JOIN SYS.INDEX_COLUMNS IC ON I.OBJECT_ID = IC.OBJECT_ID AND I.INDEX_ID = IC.INDEX_ID
JOIN SYS.COLUMNS C ON IC.OBJECT_ID = C.OBJECT_ID AND IC.COLUMN_ID = C.COLUMN_ID
WHERE IC.KEY_ORDINAL > 0 AND I.HAS_FILTER = 0
    AND I.IS_DISABLED = 0 AND I.IS_HYPOTHETICAL = 0
ORDER BY T.SCHEMA_ID, T.NAME, I.NAME, IC.KEY_ORDINAL
"""


def _table_key(table: str, schema: str) -> str:
    return table if schema.lower() == "dbo" else f"{schema}.{table}"


def _metadata_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"missing or invalid metadata field: {field}")
    return value


def _metadata_scalar(value: Any) -> Any:
    # pandas/ODBC can return numpy scalar types; normalize representation only.
    item = getattr(value, "item", None)
    return item() if callable(item) else value


def _metadata_integer(value: Any, field: str) -> int:
    value = _metadata_scalar(value)
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"missing or invalid integer metadata field: {field}")
    return int(value)


def _metadata_bool(value: Any, field: str) -> bool:
    value = _metadata_scalar(value)
    if type(value) is bool:
        return value
    if isinstance(value, Integral) and value in (0, 1):
        return value == 1
    raise ValueError(f"missing or invalid bit metadata field: {field}")


def _metadata_description(value: Any) -> str:
    # Descriptions are optional. Unlike identity/type fields they may be NULL.
    return value if isinstance(value, str) else ""


def _metadata_collation(row: Any) -> str | None:
    if "COLLATION_NAME" not in row:
        raise ValueError("missing metadata field: COLLATION_NAME")
    value = _metadata_scalar(row["COLLATION_NAME"])
    if value is None or (isinstance(value, float) and isnan(value)):
        return None
    return _metadata_text(value, "COLLATION_NAME")


def _row_table(row: Any) -> tuple[str, str, str]:
    raw_name = _metadata_text(row.get("TABLE_NAME"), "TABLE_NAME")
    schema_name = _metadata_text(row.get("SCHEMA_NAME"), "SCHEMA_NAME")
    return _table_key(raw_name, schema_name), schema_name, raw_name


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
        return require_schema_contract(state.live_schema)

    async with state.live_schema_lock:
        if not force_refresh and _schema_cache_valid(state, cache_ttl_seconds):
            assert state.live_schema is not None
            return require_schema_contract(state.live_schema)
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
        table_name, schema_name, raw_name = _row_table(row)
        if table_name in schema:
            raise ValueError(f"duplicate schema object: {schema_name}.{raw_name}")
        schema[table_name] = {
            "schema_name": schema_name,
            "qualified_name": f"{schema_name}.{raw_name}",
            "object_id": _metadata_integer(row.get("OBJECT_ID"), "OBJECT_ID"),
            "description": _metadata_description(row.get("TABLE_DESCRIPTION")),
            "columns": {},
            "foreign_keys": [],
            "primary_key": [],
            "unique_keys": {},
        }

    column_df = await _run_sql(LIVE_SCHEMA_QUERY, sql_executor)
    for _, row in column_df.iterrows():
        table_name, _, _ = _row_table(row)
        if table_name not in schema:
            raise ValueError("column references an unknown table")
        table_info = schema[table_name]
        column_name = _metadata_text(row.get("COLUMN_NAME"), "COLUMN_NAME")
        if column_name in table_info["columns"]:
            raise ValueError("duplicate column metadata")
        table_info["columns"][column_name] = {
            "data_type": _metadata_text(row.get("DATA_TYPE"), "DATA_TYPE"),
            "is_nullable": _metadata_bool(row.get("IS_NULLABLE"), "IS_NULLABLE"),
            "max_length": _metadata_integer(row.get("MAX_LENGTH"), "MAX_LENGTH"),
            "precision": _metadata_integer(row.get("NUMERIC_PRECISION"), "NUMERIC_PRECISION"),
            "scale": _metadata_integer(row.get("NUMERIC_SCALE"), "NUMERIC_SCALE"),
            "collation_name": _metadata_collation(row),
            "is_identity": _metadata_bool(row.get("IS_IDENTITY"), "IS_IDENTITY"),
            "is_computed": _metadata_bool(row.get("IS_COMPUTED"), "IS_COMPUTED"),
            "description": _metadata_description(row.get("COLUMN_DESCRIPTION")),
        }

    fk_df = await _run_sql(LIVE_FK_QUERY, sql_executor)
    for _, row in fk_df.iterrows():
        table_name, _, _ = _row_table(row)
        if table_name not in schema:
            raise ValueError("foreign key references an unknown source table")
        table_info = schema[table_name]
        table_info["foreign_keys"].append(
            {
                "column_name": _metadata_text(row.get("COLUMN_NAME"), "COLUMN_NAME"),
                "referenced_table": _table_key(
                    _metadata_text(row.get("REFERENCED_TABLE"), "REFERENCED_TABLE"),
                    _metadata_text(row.get("REFERENCED_SCHEMA"), "REFERENCED_SCHEMA"),
                ),
                "referenced_column": _metadata_text(
                    row.get("REFERENCED_COLUMN"), "REFERENCED_COLUMN"
                ),
                "constraint_name": _metadata_text(row.get("CONSTRAINT_NAME"), "CONSTRAINT_NAME"),
                "ordinal": _metadata_integer(row.get("COLUMN_ORDINAL"), "COLUMN_ORDINAL"),
                "is_disabled": _metadata_bool(row.get("IS_DISABLED"), "IS_DISABLED"),
                "is_not_trusted": _metadata_bool(row.get("IS_NOT_TRUSTED"), "IS_NOT_TRUSTED"),
            }
        )

    key_df = await _run_sql(LIVE_KEY_QUERY, sql_executor)
    keys: dict[tuple[str, str], list[tuple[int, str, bool]]] = {}
    for _, row in key_df.iterrows():
        table_name, _, _ = _row_table(row)
        if table_name not in schema:
            raise ValueError("key references an unknown table")
        name = _metadata_text(row.get("CONSTRAINT_NAME"), "CONSTRAINT_NAME")
        keys.setdefault((table_name, name), []).append(
            (
                _metadata_integer(row.get("COLUMN_ORDINAL"), "COLUMN_ORDINAL"),
                _metadata_text(row.get("COLUMN_NAME"), "COLUMN_NAME"),
                _metadata_bool(row.get("IS_PRIMARY_KEY"), "IS_PRIMARY_KEY"),
            )
        )
    for (table_name, name), entries in keys.items():
        entries.sort(key=lambda entry: entry[0])
        if [entry[0] for entry in entries] != list(range(1, len(entries) + 1)):
            raise ValueError("key metadata ordinals must be unique and contiguous")
        if any(entry[2] != entries[0][2] for entry in entries):
            raise ValueError("inconsistent primary key metadata")
        info = schema[table_name]
        key_columns = [entry[1] for entry in entries]
        info["unique_keys"][name] = key_columns
        if entries[0][2]:
            if info["primary_key"]:
                raise ValueError("multiple primary keys for a table")
            info["primary_key"] = key_columns.copy()
    require_schema_contract(schema)
    state.live_schema = schema
    state.live_schema_cached_at = time.monotonic()
    logger.info("Loaded live schema for %d tables", len(schema))
    return schema
