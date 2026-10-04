"""Select grounded memories and render bounded Prompt context blocks."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.logging import setup_logging
from ..domain.semantic_ir import QueryPlan, parse_question_semantics
from ..knowledge.schema_contract import usable_single_column_foreign_keys
from ..knowledge.source_types import STRUCTURE_ONLY_SOURCE_TYPES
from .context_state import ContextRuntimeState

logger = setup_logging("text2sql.context.rendering")

HIGH_VALUE_MEMORY_TYPES = {
    "feedback_example",
    "question_sql_example",
    "structured_question",
    "analogy_rule",
    "join_path",
    "question_template",
    "metric_rule",
    "time_rule",
    "synonym_rule",
    "alias_dict",
    "field_alias",
    "table_alias",
    "sample_values",
    "column_profile",
    "table_role",
}
MAX_SCHEMA_COLUMNS_PER_TABLE = 12
MAX_MEMORY_BLOCK_ITEMS = 8
MAX_MEMORY_LINE_LENGTH = 320


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _normalize_match_text(text: str) -> str:
    text = _normalize(text)
    text = re.sub(r"[?？。！!；;，,]+", "", text)
    return text.strip()


def _load_knowledge_index(
    index_path_value: str, state: ContextRuntimeState
) -> list[dict[str, Any]]:
    with state.knowledge_index_lock:
        return _load_knowledge_index_locked(index_path_value, state)


def warm_knowledge_index(index_path_value: str, state: ContextRuntimeState) -> None:
    """Load a cold immutable index in a worker before synchronous rendering."""
    _load_knowledge_index(index_path_value, state)


def _load_knowledge_index_locked(
    index_path_value: str, state: ContextRuntimeState
) -> list[dict[str, Any]]:
    cache_key = str(Path(index_path_value).resolve())
    if cache_key in state.knowledge_indexes:
        return state.knowledge_indexes[cache_key]
    index_path = Path(index_path_value)
    if not index_path.exists():
        state.knowledge_indexes[cache_key] = []
        return state.knowledge_indexes[cache_key]
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        state.knowledge_indexes[cache_key] = payload if isinstance(payload, list) else []
    except Exception as exc:
        logger.warning("Failed to load knowledge index: %s", exc)
        state.knowledge_indexes[cache_key] = []
    return state.knowledge_indexes[cache_key]


def _semantic_tags(ir: QueryPlan) -> dict[str, list[str]]:
    return {
        "metric_tags": [item.name for item in ir.metrics],
        "dimension_tags": list(ir.dimensions),
        "time_tags": [ir.date_column] if ir.date_column else [],
        "filter_tags": [
            value
            for intent in ir.entity_filter_intents
            for value in (intent.entity, *intent.values)
            if value
        ],
        "role_tags": [],
    }


def build_required_constraint_bundle(
    question: str, semantic_ir: QueryPlan | None = None
) -> dict[str, list[dict[str, str]]]:
    semantic_ir = semantic_ir or parse_question_semantics(question)
    constraints: dict[str, list[dict[str, str]]] = {
        "filters": [],
        "tables": [],
        "joins": [],
    }
    for intent in semantic_ir.entity_filter_intents:
        values = "、".join(intent.values)
        constraints["filters"].append(
            {
                "label": intent.entity,
                "table": intent.table,
                "column": intent.column,
                "value": values,
                "description": (
                    f"实体 {intent.entity} 必须通过 {intent.table}.{intent.column} "
                    + (
                        f"排除规范值：{values}。"
                        if intent.operator == "not_in"
                        else f"过滤为规范值：{values}。"
                    )
                ),
            }
        )
    for table in semantic_ir.required_tables:
        constraints["tables"].append(
            {"table": table, "description": f"语义目录要求 SQL 使用表 {table}。"}
        )
    for join in semantic_ir.required_joins:
        constraints["joins"].append(
            {
                "left_table": join.left_table,
                "right_table": join.right_table,
                "left_column": join.left_column,
                "right_column": join.right_column,
                "description": (
                    f"必须按 {join.left_table}.{join.left_column} = "
                    f"{join.right_table}.{join.right_column} 关联。"
                ),
            }
        )
    return constraints


def render_required_constraint_blocks(
    constraints: dict[str, list[dict[str, str]]],
) -> list[str]:
    blocks: list[str] = []
    required_filters = constraints.get("filters", [])
    required_tables = constraints.get("tables", [])
    required_joins = constraints.get("joins", [])
    if required_filters:
        lines = ["【问题显式过滤约束】"]
        lines.extend(
            f"{index}. {item['description']}"
            for index, item in enumerate(required_filters, start=1)
        )
        blocks.append("\n".join(lines))
    if required_tables or required_joins:
        lines = ["【问题显式关联约束】"]
        lines.extend(
            f"{index}. {item['description']}"
            for index, item in enumerate([*required_tables, *required_joins], start=1)
        )
        blocks.append("\n".join(lines))
    return blocks


def _token_in_question(question: str, token: str) -> bool:
    question_normalized = _normalize_match_text(question)
    token = _normalize_match_text(token)
    if not token:
        return False
    if re.search(r"[\u4e00-\u9fff]", token):
        return token in question_normalized
    return re.search(rf"\b{re.escape(token)}\b", question_normalized) is not None


def _entry_matches_question(
    entry: dict[str, Any], question: str, question_tags: dict[str, list[str]]
) -> bool:
    for field in ("aliases", "field_names", "enum_values"):
        if any(_token_in_question(question, str(value)) for value in entry.get(field, [])):
            return True
    granularity = str(entry.get("granularity") or "").strip()
    if granularity and granularity != "未识别" and granularity in question:
        return True
    return any(
        tags and set(entry.get(tag_key, []) or []).intersection(tags)
        for tag_key, tags in question_tags.items()
    )


def filter_memories(
    memory_texts: list[str],
    candidate_tables: list[str],
    limit: int = 12,
    question: str = "",
    knowledge_index_path: str = "",
    *,
    state: ContextRuntimeState,
) -> list[str]:
    if not memory_texts or not candidate_tables:
        return []
    index_by_content = {
        entry["content"]: entry
        for entry in _load_knowledge_index(knowledge_index_path, state)
        if isinstance(entry.get("content"), str)
    }
    filtered: list[str] = []
    for content in memory_texts:
        entry = index_by_content.get(content)
        if entry:
            source_type = entry.get("source_type", "")
            if source_type in STRUCTURE_ONLY_SOURCE_TYPES:
                continue
            entry_tables = set(entry.get("table_names", []))
            if entry_tables.intersection(candidate_tables) or (
                not entry_tables
                and source_type in HIGH_VALUE_MEMORY_TYPES
                and _entry_matches_question(
                    entry, question, _semantic_tags(parse_question_semantics(question))
                )
            ):
                filtered.append(content)
                continue
    return list(dict.fromkeys(filtered))[:limit]


def _select_schema_columns(
    question: str,
    table_name: str,
    table_info: dict[str, Any],
    candidate_tables: list[str],
    index_entries: list[dict[str, Any]],
    semantic_ir: QueryPlan,
) -> list[str]:
    columns = table_info.get("columns", {})
    if not columns:
        return []
    if len(columns) <= MAX_SCHEMA_COLUMNS_PER_TABLE:
        return list(columns.keys())
    question_tags = _semantic_tags(semantic_ir)
    required: list[str] = [
        item.column
        for item in semantic_ir.metrics
        if item.source_table == table_name and item.column != "*"
    ]
    required.extend(
        column
        for dimension in semantic_ir.dimensions
        for column in semantic_ir.dimension_columns.get(dimension, ())
        if column in columns
    )
    required.extend(
        intent.column for intent in semantic_ir.entity_filter_intents if intent.table == table_name
    )
    if semantic_ir.date_table == table_name and semantic_ir.date_column:
        required.append(semantic_ir.date_column)
    for join in semantic_ir.required_joins:
        if join.left_table == table_name:
            required.append(join.left_column)
        if join.right_table == table_name:
            required.append(join.right_column)
    for fk in usable_single_column_foreign_keys(table_info):
        if str(fk.get("referenced_table") or "") in candidate_tables:
            column_name = str(fk.get("column_name") or "")
            if column_name:
                required.append(column_name)
    selected: list[str] = []
    for column_name, column_info in columns.items():
        description = str(column_info.get("description") or "")
        if _token_in_question(question, column_name) or (
            description and _token_in_question(question, description)
        ):
            selected.append(column_name)
    for entry in index_entries:
        if table_name in entry.get("table_names", []) and _entry_matches_question(
            entry, question, question_tags
        ):
            selected.extend(
                str(field_name)
                for field_name in entry.get("field_names", []) or []
                if field_name in columns
            )
    required = list(dict.fromkeys(column for column in required if column in columns))
    ordered = list(
        dict.fromkeys(column for column in selected if column in columns and column not in required)
    )
    for column_name in columns:
        if column_name not in ordered and column_name not in required:
            ordered.append(column_name)
    return required + ordered[: max(0, MAX_SCHEMA_COLUMNS_PER_TABLE - len(required))]


def format_schema_block(
    question: str,
    live_schema: dict[str, dict[str, Any]],
    candidate_tables: list[str],
    knowledge_index_path: str,
    semantic_ir: QueryPlan | None = None,
    *,
    state: ContextRuntimeState,
) -> str:
    if not candidate_tables:
        return ""
    lines = [
        "【实时数据库结构约束】",
        "只允许使用下列真实存在的表和字段；如果信息不足，禁止猜测不存在的表名或字段名。",
    ]
    index_entries = _load_knowledge_index(knowledge_index_path, state)
    semantic_ir = semantic_ir or parse_question_semantics(question)
    for table_name in candidate_tables:
        table_info = live_schema.get(table_name)
        if not table_info:
            continue
        table_desc = table_info.get("description", "")
        selected_columns = _select_schema_columns(
            question, table_name, table_info, candidate_tables, index_entries, semantic_ir
        )
        if len(selected_columns) > MAX_SCHEMA_COLUMNS_PER_TABLE:
            lines.append("  - 字段预算提示: 语义必需字段超过常规上限，已完整保留")
        lines.append(f"表: {table_name}" + (f" | 描述: {table_desc}" if table_desc else ""))
        for column_name in selected_columns:
            column_info = table_info["columns"].get(column_name, {})
            column_desc = column_info.get("description", "")
            lines.append(
                f"  - 字段: {column_name} ({column_info['data_type']}, 可空:{column_info['is_nullable']})"
                + (f" | 说明: {column_desc}" if column_desc else "")
            )
        omitted_count = max(0, len(table_info["columns"]) - len(selected_columns))
        if omitted_count:
            lines.append(f"  - 其余字段省略: {omitted_count} 个")
        for fk in usable_single_column_foreign_keys(table_info):
            lines.append(f"  - 外键: {table_name}.{fk['column_name']} -> {fk['referenced_table']}")
    return "\n".join(lines)


def format_feedback_examples_block(examples: list[dict[str, str]]) -> str:
    if not examples:
        return ""
    lines = [
        "【已验证问答示例】",
        "以下示例经过执行或人工验证，优先参考其表选择、关联方式和过滤写法。",
    ]
    for index, example in enumerate(examples, start=1):
        lines.append(f"{index}. 问题: {example['question']}")
        lines.append(f"   SQL: {example['sql']}")
    return "\n".join(lines)


def format_negative_examples_block(examples: list[dict[str, Any]]) -> str:
    if not examples:
        return ""
    lines = ["【相似错误案例（禁止复现）】"]
    for index, item in enumerate(examples, start=1):
        errors = ", ".join(item.get("error_types", []) or []) or str(
            item.get("comment") or "语义不正确"
        )
        lines.append(f"{index}. 错误 SQL: {str(item.get('sql') or '')[:500]}")
        lines.append(f"   错误原因: {errors}")
    return "\n".join(lines)


def format_memory_block(
    memory_texts: list[str],
    knowledge_index_path: str,
    *,
    state: ContextRuntimeState,
) -> str:
    if not memory_texts:
        return ""
    index_by_content = {
        entry["content"]: entry
        for entry in _load_knowledge_index(knowledge_index_path, state)
        if isinstance(entry.get("content"), str)
    }
    trusted = [content for content in memory_texts if content in index_by_content]
    if not trusted:
        return ""
    lines = ["【训练知识与业务规则】"]
    for content in trusted[:MAX_MEMORY_BLOCK_ITEMS]:
        entry = index_by_content[content]
        source_type = str(entry.get("source_type") or "").strip()
        compact = re.sub(r"\s+", " ", content).strip()
        if len(compact) > MAX_MEMORY_LINE_LENGTH:
            compact = compact[: MAX_MEMORY_LINE_LENGTH - 3].rstrip() + "..."
        prefix = f"[{source_type}] " if source_type else ""
        lines.append(f"- {prefix}{compact}")
    omitted_count = max(0, len(trusted) - MAX_MEMORY_BLOCK_ITEMS)
    if omitted_count:
        lines.append(f"- 其余训练记忆省略: {omitted_count} 条")
    return "\n".join(lines)
