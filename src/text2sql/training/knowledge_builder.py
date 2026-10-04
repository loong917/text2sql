"""Compile validated business knowledge and reviewed examples into memories."""

from __future__ import annotations

import json
from typing import Any

from sqlglot.errors import ParseError

from ..core.logging import setup_logging
from ..domain.semantic_ir import SemanticCatalog
from ..knowledge import KnowledgeBundle
from ..knowledge.schema_contract import usable_single_column_foreign_keys
from .records import (
    extract_sql_table_names,
    metric_tags_from_metadata,
    question_aliases,
    question_sql_metadata,
    should_include_table,
    time_tags_from_metadata,
)
from .storage import save_training_text

logger = setup_logging("text2sql.training.builder")


async def train_question_sql_examples(
    knowledge_memory,
    ctx,
    question_pairs: list[tuple[str, str]],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    catalog: SemanticCatalog,
    allowed_tables: set[str] | None,
) -> None:
    report["question_sql_examples_trained"] = len(question_pairs)
    for question, sql in question_pairs:
        metadata = question_sql_metadata(question, sql, catalog, allowed_tables)
        example_text = f"中文问题与 SQL 示例:\n问题: {question}\nSQL Server SQL:\n{sql}"
        await save_training_text(
            knowledge_memory,
            example_text,
            ctx,
            index_records,
            source_type="question_sql_example",
            table_names=metadata["sql_tables"],
            aliases=question_aliases(question),
            metric_tags=metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            join_tables=metadata["sql_tables"],
            confidence=95,
        )


async def train_join_paths(live_schema, knowledge_memory, ctx, index_records, allowed_tables):
    logger.info("正在训练 join 路径...")
    for table_name, info in sorted(live_schema.items()):
        if not should_include_table(table_name, allowed_tables):
            continue
        for fk in usable_single_column_foreign_keys(info):
            referenced_table = str(fk.get("referenced_table") or "")
            if not referenced_table or not should_include_table(referenced_table, allowed_tables):
                continue
            column_name = str(fk.get("column_name") or "")
            referenced_column = str(fk.get("referenced_column") or "")
            if not referenced_column:
                # Incomplete FK metadata must not manufacture a target key.
                continue
            join_text = (
                "推荐关联路径:\n"
                f"事实表: {table_name}\n"
                f"关联字段: {column_name}\n"
                f"维度表: {referenced_table}\n"
                f"推荐写法: {table_name}.{column_name} = {referenced_table}.{referenced_column}"
            )
            await save_training_text(
                knowledge_memory,
                join_text,
                ctx,
                index_records,
                source_type="join_path",
                table_names=[table_name, referenced_table],
                field_names=[column_name] if column_name else None,
                join_tables=[table_name, referenced_table],
                confidence=90,
            )


async def train_feedback_examples(
    knowledge_memory,
    ctx,
    index_records,
    report,
    bundle: KnowledgeBundle,
    feedback_examples: list[dict[str, Any]],
    raw_count: int,
    allowed_tables: set[str] | None,
):
    logger.info("正在加载运行期反馈正确样本...")
    report["feedback_examples_loaded"] = len(feedback_examples)
    report["feedback_examples_rejected"] = max(raw_count - len(feedback_examples), 0)
    if not feedback_examples:
        return
    for item in feedback_examples:
        question = str(item.get("question") or "").strip()
        sql = str(item.get("sql") or "").strip()
        if not question or not sql:
            continue
        metadata = question_sql_metadata(question, sql, bundle.semantic_catalog(), allowed_tables)
        await save_training_text(
            knowledge_memory,
            f"运行期反馈正确样本:\n问题: {question}\nSQL:\n{sql}",
            ctx,
            index_records,
            source_type="feedback_example",
            table_names=metadata["sql_tables"],
            aliases=question_aliases(question),
            metric_tags=metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            join_tables=metadata["sql_tables"],
            confidence=98,
        )
        report["feedback_examples"] += 1


async def train_structured_knowledge(
    knowledge_memory,
    ctx,
    bundle: KnowledgeBundle,
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    allowed_tables: set[str] | None,
) -> None:
    """Train only typed, schema-validated knowledge records."""
    report["knowledge_source"] = "structured"
    report["structured_files"] = len(bundle.files)
    report["structured_records_rejected"] = len(bundle.errors)
    report["warnings"].extend(bundle.errors)
    trained = 0

    for card in bundle.table_cards:
        table = str(card["table"])
        text = (
            f"表卡片: {table}\n业务含义: {card.get('description', '')}\n"
            f"业务别名: {'、'.join(card.get('business_aliases', []))}\n"
            f"指标: {'、'.join(card.get('metrics', []))}\n"
            f"维度: {'、'.join(card.get('dimensions', []))}\n"
            f"关键字段: {'、'.join(card.get('important_columns', []))}"
        )
        await save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="table_card",
            table_names=[table],
            field_names=card.get("important_columns", []),
            aliases=card.get("business_aliases", []),
            metric_tags=card.get("metrics", []),
            dimension_tags=card.get("dimensions", []),
            confidence=95,
        )
        trained += 1

    for metric in bundle.metrics:
        table = str(metric["source_table"])
        column = str(metric.get("column") or metric.get("expression"))
        text = (
            f"指标定义: {metric.get('name')}\n别名: {'、'.join(metric.get('aliases', []))}\n"
            f"计算: {metric.get('aggregation')}({table}.{column})\n"
            f"单位: {metric.get('unit', '')}"
        )
        await save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="metric_rule",
            table_names=[table],
            field_names=[column],
            aliases=metric.get("aliases", []),
            metric_tags=[str(metric.get("id") or metric.get("name"))],
            confidence=98,
        )
        trained += 1

    for dimension in bundle.dimensions:
        table = str(dimension.get("table") or "")
        text = (
            f"维度定义: {dimension.get('name')}\n"
            f"别名: {'、'.join(dimension.get('aliases', []))}\n"
            f"来源: {table}.{','.join(dimension.get('columns', []))}"
        )
        await save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="dimension_rule",
            table_names=[table],
            field_names=dimension.get("columns", []),
            aliases=dimension.get("aliases", []),
            dimension_tags=[str(dimension.get("id") or dimension.get("name"))],
            confidence=96,
        )
        trained += 1

    for join in bundle.joins:
        tables = [str(join["left_table"]), str(join["right_table"])]
        text = (
            f"业务关联: {join['left_table']}.{join['left_column']} = "
            f"{join['right_table']}.{join['right_column']}\n"
            f"用途: {join.get('purpose', '')}"
        )
        await save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="join_path",
            table_names=tables,
            join_tables=tables,
            field_names=[str(join["left_column"]), str(join["right_column"])],
            confidence=99,
        )
        trained += 1

    for policy in bundle.policies:
        text = "业务策略: " + json.dumps(policy, ensure_ascii=False, sort_keys=True)
        await save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="domain_policy",
            table_names=[str(policy["table"])] if policy.get("table") else None,
            field_names=[str(policy["column"])] if policy.get("column") else None,
            aliases=policy.get("terms", []),
            confidence=98,
        )
        trained += 1

    pairs = [
        (str(item["question"]), str(item["sql"]))
        for item in bundle.gold_sql
        if item.get("status") == "approved"
    ]
    await train_question_sql_examples(
        knowledge_memory,
        ctx,
        pairs,
        index_records,
        report,
        bundle.semantic_catalog(),
        allowed_tables,
    )
    trained += len(pairs)
    for negative in bundle.negative_sql:
        if negative.get("status") != "approved":
            continue
        sql = str(negative.get("sql") or "")
        try:
            negative_tables = extract_sql_table_names(sql)
        except ParseError:
            negative_tables = []  # Syntax-error examples are deliberately invalid.
        await save_training_text(
            knowledge_memory,
            f"已审核错误示例（禁止复现）\n问题: {negative.get('question')}\n错误 SQL: {sql}\n"
            f"错误类型: {', '.join(negative.get('error_types', []))}",
            ctx,
            index_records,
            source_type="negative_sql_example",
            table_names=negative_tables,
            aliases=question_aliases(str(negative.get("question") or "")),
            confidence=98,
        )
        trained += 1
    report["negative_sql_examples_trained"] = sum(
        item.get("status") == "approved" for item in bundle.negative_sql
    )
    for refusal in bundle.refusals:
        if refusal.get("status") != "approved":
            continue
        await save_training_text(
            knowledge_memory,
            f"拒答示例\n问题: {refusal.get('question')}\n原因: {refusal.get('reason')}",
            ctx,
            index_records,
            source_type="refusal_example",
            confidence=98,
        )
        trained += 1
    report["question_sql_examples"] = len(pairs)
    report["structured_records"] = trained
