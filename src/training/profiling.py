"""Allowlist-only data profiling used by offline knowledge builds."""

from __future__ import annotations

from typing import Any

from ..application.ports import SqlExecutor
from ..core.config import Settings
from ..core.logging import setup_logging

logger = setup_logging("text2sql.training.profiling")


async def _distinct_values(executor: SqlExecutor, table: str, column: str, limit: int) -> list[str]:
    sql = (
        f"SELECT TOP {limit} CAST([{column}] AS NVARCHAR(255)) AS value "
        f"FROM [{table}] WHERE [{column}] IS NOT NULL "
        f"GROUP BY [{column}] ORDER BY COUNT(1) DESC"
    )
    frame = await executor.execute(sql, timeout_seconds=30.0)
    if frame.empty:
        return []
    return [str(value).strip() for value in frame["value"].tolist() if str(value).strip()]


async def _min_max(executor: SqlExecutor, table: str, column: str) -> tuple[str, str]:
    sql = (
        f"SELECT MIN([{column}]) AS min_value, MAX([{column}]) AS max_value "
        f"FROM [{table}] WHERE [{column}] IS NOT NULL"
    )
    frame = await executor.execute(sql, timeout_seconds=30.0)
    if frame.empty:
        return "", ""
    row = frame.iloc[0]
    return str(row.get("min_value") or ""), str(row.get("max_value") or "")


def _profile_modes(column_name: str, data_type: str) -> tuple[list[str], int]:
    name = column_name.lower()
    dtype = data_type.lower()
    modes: list[str] = []
    score = 0
    if (
        "date" in name
        or "time" in name
        or dtype
        in {
            "date",
            "datetime",
            "datetime2",
            "smalldatetime",
        }
    ):
        modes.append("time_range")
        score += 100
    if dtype in {"char", "nchar", "varchar", "nvarchar"}:
        modes.append("categorical")
        score += 80
    if dtype in {
        "int",
        "bigint",
        "smallint",
        "tinyint",
        "decimal",
        "numeric",
        "float",
        "real",
        "money",
        "smallmoney",
    }:
        modes.append("numeric_range")
        score += 60
    return list(dict.fromkeys(modes)), score


def _allowed(config: Settings, table: str, column: str) -> bool:
    qualified = f"{table}.{column}".lower()
    wildcard = f"*.{column}".lower()
    allowlist = {
        item.strip().lower() for item in config.profiling_allowed_columns.split(",") if item.strip()
    }
    denylist = {
        item.strip().lower() for item in config.profiling_denied_columns.split(",") if item.strip()
    }
    return (
        qualified not in denylist
        and wildcard not in denylist
        and bool(allowlist)
        and (qualified in allowlist or wildcard in allowlist)
    )


def _select_columns(
    config: Settings, table: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        column = str(row.get("COLUMN_NAME") or "")
        data_type = str(row.get("DATA_TYPE") or "")
        if not column or not _allowed(config, table, column):
            continue
        modes, score = _profile_modes(column, data_type)
        if modes:
            candidates.append(
                {"column_name": column, "data_type": data_type, "modes": modes, "score": score}
            )
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["column_name"])))
    return candidates[: max(1, config.profiling_max_columns_per_table)]


async def train_sample_profiles(
    *,
    executor: SqlExecutor,
    knowledge_memory: Any,
    context: Any,
    sample_rows: int,
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    live_schema: dict[str, dict[str, Any]],
    config: Settings,
    allowed_tables: set[str] | None,
    save_training_text: Any,
) -> None:
    """Profile only explicitly allowlisted columns and save bounded summaries."""
    table_columns: dict[str, list[dict[str, Any]]] = {}
    for table, info in live_schema.items():
        if allowed_tables is not None and table not in allowed_tables:
            continue
        table_columns[table] = [
            {
                "COLUMN_NAME": column,
                "DATA_TYPE": str(details.get("data_type") or ""),
            }
            for column, details in info.get("columns", {}).items()
        ]
    configured_order = [
        item.strip() for item in (config.sample_tables or "").split(",") if item.strip()
    ]
    table_order = (
        [table for table in configured_order if table in table_columns]
        if configured_order
        else sorted(table_columns)
    )
    selected = {
        table: columns
        for table in table_order
        if (columns := _select_columns(config, table, table_columns[table]))
    }
    ordered_tables = [table for table in table_order if table in selected]
    if config.profiling_max_tables > 0:
        ordered_tables = ordered_tables[: config.profiling_max_tables]
    report["profiling_tables_considered"] = len(ordered_tables)
    report["profiling_columns_selected"] = sum(len(selected[table]) for table in ordered_tables)

    profile_records = 0
    for table in ordered_tables:
        try:
            for item in selected[table]:
                column = str(item["column_name"])
                data_type = str(item["data_type"])
                modes = list(item["modes"])
                if "categorical" in modes:
                    values = await _distinct_values(
                        executor,
                        table,
                        column,
                        min(sample_rows, config.profiling_max_distinct_values),
                    )
                    if values:
                        await save_training_text(
                            knowledge_memory,
                            f"字段画像:\n表: {table}\n字段: {column}\n类型: {data_type}\n"
                            f"高频离散值: {'、'.join(values)}",
                            context,
                            index_records,
                            source_type="sample_values",
                            table_names=[table],
                            field_names=[column],
                            aliases=values,
                            enum_values=values,
                            profile_tags=["categorical"],
                            confidence=74,
                        )
                        profile_records += 1
                for mode, label, confidence, tag_field in (
                    ("time_range", "时间范围", 72, "time_tags"),
                    ("numeric_range", "数值范围", 70, "metric_tags"),
                ):
                    if mode not in modes:
                        continue
                    minimum, maximum = await _min_max(executor, table, column)
                    if not minimum and not maximum:
                        continue
                    metadata = {tag_field: [column]}
                    await save_training_text(
                        knowledge_memory,
                        f"字段画像:\n表: {table}\n字段: {column}\n类型: {data_type}\n"
                        f"{label}: {minimum} ~ {maximum}",
                        context,
                        index_records,
                        source_type="column_profile",
                        table_names=[table],
                        field_names=[column],
                        profile_tags=[mode],
                        confidence=confidence,
                        **metadata,
                    )
                    profile_records += 1
        except Exception as exc:
            logger.warning("训练字段画像失败 %s: %s", table, exc)
            report["warnings"].append(f"字段画像失败 {table}: {exc}")
    report["profile_records"] = profile_records
