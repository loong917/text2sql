"""Pure record compilation and deduplication for versioned knowledge."""

from __future__ import annotations

import re
from typing import Any

from ..domain.semantic_ir import SemanticCatalog, parse_question_semantics
from ..knowledge.schema_contract import usable_single_column_foreign_keys
from ..retrieval.dataset import extract_sql_tables


def build_index_record(
    text: str,
    source_type: str,
    table_names: list[str] | None = None,
    field_names: list[str] | None = None,
    aliases: list[str] | None = None,
    metric_tags: list[str] | None = None,
    dimension_tags: list[str] | None = None,
    time_tags: list[str] | None = None,
    filter_tags: list[str] | None = None,
    join_tables: list[str] | None = None,
    role_tags: list[str] | None = None,
    profile_tags: list[str] | None = None,
    enum_values: list[str] | None = None,
    granularity: str | None = None,
    confidence: int = 50,
) -> dict[str, Any]:
    return {
        "content": text.strip(),
        "source_type": source_type,
        "table_names": dedupe_keep_order(table_names or []),
        "field_names": dedupe_keep_order(field_names or []),
        "aliases": dedupe_keep_order(aliases or []),
        "metric_tags": dedupe_keep_order(metric_tags or []),
        "dimension_tags": dedupe_keep_order(dimension_tags or []),
        "time_tags": dedupe_keep_order(time_tags or []),
        "filter_tags": dedupe_keep_order(filter_tags or []),
        "join_tables": dedupe_keep_order(join_tables or []),
        "role_tags": dedupe_keep_order(role_tags or []),
        "profile_tags": dedupe_keep_order(profile_tags or []),
        "enum_values": dedupe_keep_order(enum_values or []),
        "granularity": (granularity or "").strip(),
        "confidence": confidence,
    }


def append_index_record(
    index_records: list[dict[str, Any]],
    text: str,
    *,
    source_type: str,
    table_names: list[str] | None = None,
    field_names: list[str] | None = None,
    aliases: list[str] | None = None,
    metric_tags: list[str] | None = None,
    dimension_tags: list[str] | None = None,
    time_tags: list[str] | None = None,
    filter_tags: list[str] | None = None,
    join_tables: list[str] | None = None,
    role_tags: list[str] | None = None,
    profile_tags: list[str] | None = None,
    enum_values: list[str] | None = None,
    granularity: str | None = None,
    confidence: int = 50,
) -> None:
    content = text.strip()
    if not content:
        return
    index_records.append(
        build_index_record(
            content,
            source_type=source_type,
            table_names=table_names,
            field_names=field_names,
            aliases=aliases,
            metric_tags=metric_tags,
            dimension_tags=dimension_tags,
            time_tags=time_tags,
            filter_tags=filter_tags,
            join_tables=join_tables,
            role_tags=role_tags,
            profile_tags=profile_tags,
            enum_values=enum_values,
            granularity=granularity,
            confidence=confidence,
        )
    )


def dedupe_keep_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def first_sentence(text: str) -> str:
    return re.split(r"[。；;，,]", text.strip(), maxsplit=1)[0].strip()


def field_aliases(description: str) -> list[str]:
    core = first_sentence(description)
    aliases = [core]

    if core.endswith("名称"):
        aliases.append(core[:-2] + "名")
    if core.endswith("编号"):
        aliases.append(core[:-2] + "ID")
    if core.endswith("日期"):
        aliases.append(core[:-2] + "时间")
    if core.startswith("是否"):
        aliases.append(core[2:])

    return dedupe_keep_order(aliases)


def question_sql_metadata(
    question: str,
    sql: str,
    catalog: SemanticCatalog,
    allowed_tables: set[str] | None,
) -> dict[str, Any]:
    semantic_ir = parse_question_semantics(question, catalog)
    sql_tables = [
        table
        for table in extract_sql_table_names(sql)
        if should_include_table(table, allowed_tables)
    ]
    return {
        "dimensions": list(semantic_ir.dimensions),
        "metric": ",".join(metric.name for metric in semantic_ir.metrics) or "未识别",
        "time_expression": (
            f"{semantic_ir.date_start} ~ {semantic_ir.date_end}"
            if semantic_ir.date_start and semantic_ir.date_end
            else "未显式说明"
        ),
        "filters": [
            f"{intent.entity}:{value}"
            for intent in semantic_ir.entity_filter_intents
            for value in intent.values
        ],
        "sql_tables": sql_tables,
    }


def normalize_training_question(question: str) -> str:
    question = re.sub(r"\s+", " ", str(question or "")).strip()
    question = re.sub(r"^[0-9]+[\.\-、:：\s]+", "", question)
    question = re.sub(r"[?？。！!；;]+$", "", question).strip()
    return question


def question_aliases(question: str) -> list[str]:
    normalized = normalize_training_question(question)
    aliases = [str(question or "").strip(), normalized]
    if normalized:
        aliases.append(f"{normalized}?")
        aliases.append(f"{normalized}？")
    return dedupe_keep_order([alias for alias in aliases if alias])


def extract_sql_table_names(sql: str) -> list[str]:
    """Use SQL scope-aware table extraction rather than keyword matches."""
    return list(extract_sql_tables(sql))


def metric_tags_from_metadata(metadata: dict[str, Any]) -> list[str] | None:
    metric = str(metadata.get("metric") or "").strip()
    return [metric] if metric and metric != "未识别" else None


def time_tags_from_metadata(metadata: dict[str, Any]) -> list[str] | None:
    time_expression = str(metadata.get("time_expression") or "").strip()
    return [time_expression] if time_expression and time_expression != "未显式说明" else None


def should_include_table(table_name: str, allowed_tables: set[str] | None) -> bool:
    return allowed_tables is None or table_name in allowed_tables


def merge_index_values(existing: list[str], incoming: list[str]) -> list[str]:
    return dedupe_keep_order([*(existing or []), *(incoming or [])])


def dedupe_index_records(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    ordered_keys: list[tuple[str, str]] = []

    for record in records:
        source_type = str(record.get("source_type") or "")
        content = str(record.get("content") or "").strip()
        if not source_type or not content:
            continue
        key = (source_type, content)
        if key not in deduped:
            deduped[key] = dict(record)
            ordered_keys.append(key)
            continue

        existing = deduped[key]
        for field in [
            "table_names",
            "field_names",
            "aliases",
            "metric_tags",
            "dimension_tags",
            "time_tags",
            "filter_tags",
            "join_tables",
            "role_tags",
            "profile_tags",
            "enum_values",
        ]:
            existing[field] = merge_index_values(
                list(existing.get(field, []) or []),
                list(record.get(field, []) or []),
            )
        existing["confidence"] = max(
            int(existing.get("confidence", 0) or 0),
            int(record.get("confidence", 0) or 0),
        )
        if not existing.get("granularity") and record.get("granularity"):
            existing["granularity"] = record.get("granularity")

    deduped_records = [deduped[key] for key in ordered_keys]
    return deduped_records, max(0, len(records) - len(deduped_records))


def build_table_schema_index_records(
    live_schema: dict[str, dict[str, Any]],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    allowed_tables: set[str] | None,
) -> None:
    table_count = 0
    column_count = 0
    for table_name, info in sorted(live_schema.items()):
        if not should_include_table(table_name, allowed_tables):
            continue
        description = str(info.get("description") or "")
        columns = info.get("columns", {})
        field_names = list(columns.keys())
        aliases = [description] if description else None
        append_index_record(
            index_records,
            f"表: {table_name} 描述: {description}",
            source_type="table_description",
            table_names=[table_name],
            aliases=aliases,
            confidence=72,
        )
        append_index_record(
            index_records,
            "表名: {table_name}\n字段列表:\n{field_block}".format(
                table_name=table_name,
                field_block="\n".join(
                    "  - 字段: {name} ({dtype}, 可空:{nullable}){suffix}".format(
                        name=column_name,
                        dtype=column_info.get("data_type", ""),
                        nullable=column_info.get("is_nullable", False),
                        suffix=(
                            f" - {column_info.get('description', '')}"
                            if column_info.get("description")
                            else ""
                        ),
                    )
                    for column_name, column_info in columns.items()
                ),
            ),
            source_type="column_schema",
            table_names=[table_name],
            field_names=field_names,
            aliases=dedupe_keep_order(
                [
                    alias
                    for column_info in columns.values()
                    for alias in field_aliases(str(column_info.get("description", "")))
                    if column_info.get("description")
                ]
            ),
            confidence=82,
        )
        for fk in usable_single_column_foreign_keys(info):
            referenced_table = str(fk.get("referenced_table") or "")
            if not referenced_table or not should_include_table(referenced_table, allowed_tables):
                continue
            column_name = str(fk.get("column_name") or "")
            append_index_record(
                index_records,
                f"外键关系: {table_name}.{column_name} -> {referenced_table}",
                source_type="foreign_key",
                table_names=[table_name, referenced_table],
                field_names=[column_name] if column_name else None,
                join_tables=[table_name, referenced_table],
                confidence=78,
            )
        table_count += 1
        column_count += len(field_names)
    report["table_count"] = table_count
    report["column_count"] = column_count
