"""Compose grounded prompts through replaceable application ports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..domain.semantic_ir import QueryPlan, parse_question_semantics, render_semantic_ir
from ..domain.sql_validation import SqlSafetyPolicy, validate_tsql_ast
from .constants import REFUSAL_TOKEN
from .context_rendering import (
    build_required_constraint_bundle,
    filter_memories,
    format_feedback_examples_block,
    format_memory_block,
    format_negative_examples_block,
    format_schema_block,
    render_required_constraint_blocks,
    warm_knowledge_index,
)
from .context_state import ContextRuntimeState
from .contracts import QueryContext
from .ports import (
    ArtifactProvider,
    FeedbackRepository,
    KnowledgeMemory,
    SchemaRepository,
    SemanticParser,
    TableSelector,
)
from .sql_policy import SqlValidationConfig


@dataclass(frozen=True)
class ContextServiceConfig:
    knowledge_index_path: str
    prompt_feedback_examples: int
    prompt_token_budget: int = 6000
    use_live_feedback: bool = True


async def build_prompt_context(
    question: str,
    config: ContextServiceConfig,
    *,
    feedback_repository: FeedbackRepository,
    schema_repository: SchemaRepository,
    knowledge_memory: KnowledgeMemory,
    artifact_provider: ArtifactProvider,
    semantic_parser: SemanticParser,
    table_selector: TableSelector,
    state: ContextRuntimeState,
) -> QueryContext:
    schema = await schema_repository.get()
    knowledge = artifact_provider.get()
    if schema.fingerprint != knowledge.schema_fingerprint:
        raise ValueError("live Schema changed; rebuild and evaluate the knowledge artifact")
    plan = semantic_parser.parse(question, knowledge)
    if not plan.is_ready:
        reason = "；".join(plan.diagnostics) or "问题缺少可确认的业务语义"
        return QueryContext(
            "",
            schema.tables,
            plan,
            insufficient_context=True,
            insufficiency_reason=reason,
            outcome="clarification_required"
            if str(plan.status) == "clarification_required"
            else "refused",
            diagnostics={"semantic_ir": plan.to_dict()},
        )
    await asyncio.to_thread(warm_knowledge_index, config.knowledge_index_path, state)
    memories, selection = await asyncio.gather(
        knowledge_memory.search(question, 24),
        table_selector.retrieve_with_diagnostics(question, plan, schema.tables),
    )
    candidates = list(selection.candidates)
    names = [item.table_name for item in candidates]
    reasons = {
        item.table_name: [
            {
                "type": item.source,
                "detail": item.reason,
                "probability": item.probability,
                "raw_score": item.raw_score,
                "is_bridge": item.is_bridge,
            }
        ]
        for item in candidates
    }
    filtered = filter_memories(
        memories,
        names,
        question=question,
        knowledge_index_path=config.knowledge_index_path,
        state=state,
    )
    constraints = build_required_constraint_bundle(question, plan)
    schema_block = format_schema_block(
        question,
        schema.tables,
        names,
        config.knowledge_index_path,
        plan,
        state=state,
    )
    if config.use_live_feedback:
        gold, negatives = await asyncio.gather(
            asyncio.to_thread(
                feedback_repository.search_gold,
                question,
                config.prompt_feedback_examples,
                current_schema_fingerprint=schema.fingerprint,
            ),
            asyncio.to_thread(feedback_repository.search_negative, question, 2),
        )
    else:
        # Production prompts only consume the evaluated snapshot. Reviews are
        # collected online but require a new build/evaluation/release to apply.
        gold = [
            item
            for item in knowledge.gold_examples
            if set(item.get("tables", [])).intersection(names)
        ][: config.prompt_feedback_examples]
        negatives = []
    # Snapshot negatives and, outside production, runtime feedback share rendering.
    terms = set(plan.required_tables)
    approved_negatives = [
        item
        for item in knowledge.negative_examples
        if any(table.lower() in str(item.get("sql", "")).lower() for table in terms)
    ][:2]
    negatives = [*negatives, *approved_negatives]
    required = [
        render_semantic_ir(plan),
        schema_block,
        "【生成要求】",
        "必须使用 SQL Server 只读语法；只能使用目录给定表列；语义计划不得遗漏或改写。",
        f"上下文无法证明查询正确时返回 {REFUSAL_TOKEN}。",
        *render_required_constraint_blocks(constraints),
    ]
    optional = [
        format_memory_block(filtered, config.knowledge_index_path, state=state),
        format_feedback_examples_block(gold),
        format_negative_examples_block(negatives),
    ]
    # UTF-8 bytes provide a conservative upper bound, not the previous chars/4 heuristic.
    required_prompt = "\n\n".join(part for part in required if part)
    budget_exceeded = len((question + required_prompt).encode("utf-8")) > config.prompt_token_budget
    prompt = required_prompt
    for block in optional:
        proposed = prompt + "\n\n" + block if block else prompt
        if len((question + proposed).encode("utf-8")) <= config.prompt_token_budget:
            prompt = proposed
    diagnostics = {
        "semantic_ir": plan.to_dict(),
        "retrieval": selection.diagnostics,
        "prompt_budget": config.prompt_token_budget,
        "prompt_budget_basis": "utf8_byte_upper_bound",
        "prompt_estimated_tokens": len((question + prompt).encode("utf-8")),
        "prompt_budget_exceeded": budget_exceeded,
        "feedback_source": "live_reviewed" if config.use_live_feedback else "frozen_snapshot",
    }
    insufficient = not names or budget_exceeded
    reason = (
        "必需上下文超过模型预算，请缩小查询范围"
        if budget_exceeded
        else "未召回到可证明查询所需的表"
        if not names
        else ""
    )
    return QueryContext(
        prompt,
        schema.tables,
        plan,
        names,
        {
            item.table_name: item.probability if item.probability is not None else item.raw_score
            for item in candidates
        },
        reasons,
        insufficient,
        reason,
        "clarification_required" if budget_exceeded else "refused",
        diagnostics,
    )


def validate_sql(
    sql: str,
    live_schema: dict[str, dict[str, Any]],
    question: str = "",
    semantic_ir: QueryPlan | None = None,
    *,
    config: SqlValidationConfig,
) -> str | None:
    """Validate generated T-SQL against Schema and parsed question semantics."""
    semantic_ir = semantic_ir or parse_question_semantics(question)
    policy = SqlSafetyPolicy(
        allowed_schemas=tuple(
            item.strip() for item in config.allowed_schemas.split(",") if item.strip()
        ),
        max_joins=config.max_joins,
        max_subqueries=config.max_subqueries,
        allowed_tables=tuple(
            item.strip() for item in config.allowed_tables.split(",") if item.strip()
        ),
        denied_tables=tuple(
            item.strip() for item in config.denied_tables.split(",") if item.strip()
        ),
        denied_columns=tuple(
            item.strip() for item in config.denied_columns.split(",") if item.strip()
        ),
        aggregation_only_tables=tuple(
            item.strip() for item in config.aggregation_only_tables.split(",") if item.strip()
        ),
        allow_select_star=config.allow_select_star,
        require_table=config.require_table,
        allow_cross_join=config.allow_cross_join,
    )
    return validate_tsql_ast(sql, live_schema, semantic_ir, policy)
