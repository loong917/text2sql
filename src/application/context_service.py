"""Build grounded generation context from live Schema and structured knowledge.

The module caches SQL Server metadata, retrieves semantic memories, invokes the
learned table retriever, selects columns under a token budget, renders prompt
constraints, and delegates final SQL validation to the domain layer. It does
not call the LLM or execute user-generated SQL.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from vanna import ToolContext
from vanna.core.user.models import User

from ..core.logging import setup_logging
from ..domain.semantic_ir import (
    QuestionSemanticIR,
    parse_question_semantics,
    render_semantic_ir,
)
from ..domain.sql_validation import SqlSafetyPolicy, validate_tsql_ast
from ..knowledge import load_validated_knowledge_bundle
from ..knowledge.provenance import schema_fingerprint
from ..retrieval import TableRetriever
from ..retrieval.table_retriever import OllamaEmbedder
from .constants import REFUSAL_TOKEN
from .context_rendering import (
    build_required_constraint_bundle,
    filter_memories,
    format_feedback_examples_block,
    format_memory_block,
    format_negative_examples_block,
    format_schema_block,
    render_required_constraint_blocks,
)
from .context_state import ContextRuntimeState
from .ports import FeedbackRepository, SqlExecutor
from .schema_repository import get_live_schema
from .sql_policy import SqlValidationConfig

logger = setup_logging("text2sql.context")


@dataclass(frozen=True)
class ContextServiceConfig:
    knowledge_index_path: str
    structured_knowledge_dir: str
    schema_cache_ttl_seconds: int
    prompt_feedback_examples: int
    llm_host: str = "http://localhost:11434"
    llm_timeout_seconds: float = 180.0
    llm_keep_alive: str = "15m"
    embedding_model: str = "bge-m3"
    table_retrieval_calibrator_path: str = ""
    table_retrieval_token_budget: int = 2400
    table_retrieval_require_calibration: bool = True
    table_retrieval_dataset_fingerprint: str | None = None


async def retrieve_memories(question: str, limit: int = 20, *, knowledge_memory: Any) -> list[str]:
    """Retrieve semantic memories; structural records come from live Schema."""
    context = ToolContext(
        user=User(id="query", username="query"),
        conversation_id="schema-service",
        request_id=str(uuid.uuid4()),
        agent_memory=knowledge_memory,
    )
    results = await knowledge_memory.search_text_memories(
        query=question, context=context, limit=limit
    )
    return list(dict.fromkeys(item.memory.content for item in results))[:limit]


async def build_prompt_context(
    question: str,
    config: ContextServiceConfig,
    *,
    feedback_repository: FeedbackRepository,
    sql_executor: SqlExecutor,
    knowledge_memory: Any,
    state: ContextRuntimeState,
) -> dict[str, Any]:
    """Build the grounded prompt bundle and expose retrieval diagnostics."""
    live_schema = await get_live_schema(
        config.schema_cache_ttl_seconds, sql_executor=sql_executor, state=state
    )
    if state.semantic_catalog is None:
        bundle = load_validated_knowledge_bundle(config.structured_knowledge_dir, live_schema)
        state.semantic_catalog = bundle.semantic_catalog()
    semantic_ir = parse_question_semantics(question, state.semantic_catalog)
    if state.table_retriever is None:
        state.table_retriever = TableRetriever(
            embedder=OllamaEmbedder(
                host=config.llm_host,
                timeout_seconds=config.llm_timeout_seconds,
                model=config.embedding_model,
                keep_alive=config.llm_keep_alive,
            ),
            calibrator_path=config.table_retrieval_calibrator_path,
            token_budget=config.table_retrieval_token_budget,
            embedding_model=config.embedding_model,
            require_calibration=config.table_retrieval_require_calibration,
            dataset_fingerprint=config.table_retrieval_dataset_fingerprint,
        )
    memory_texts, table_candidates = await asyncio.gather(
        retrieve_memories(question, limit=24, knowledge_memory=knowledge_memory),
        state.table_retriever.retrieve(question, semantic_ir, live_schema),
    )
    candidate_tables = [item.table_name for item in table_candidates]
    scores = {
        item.table_name: (item.probability if item.probability is not None else item.raw_score)
        for item in table_candidates
    }
    score_reasons = {
        item.table_name: [
            {
                "type": item.source,
                "detail": item.reason,
                "probability": item.probability,
                "raw_score": item.raw_score,
                "is_bridge": item.is_bridge,
            }
        ]
        for item in table_candidates
    }
    constraints = build_required_constraint_bundle(question, semantic_ir)
    filtered_memories = filter_memories(
        memory_texts,
        candidate_tables,
        question=question,
        knowledge_index_path=config.knowledge_index_path,
        state=state,
    )
    insufficient_context = not candidate_tables
    insufficiency_reason = (
        "没有召回到语义必需表或达到校准概率阈值的候选表。" if insufficient_context else ""
    )
    schema_block = format_schema_block(
        question,
        live_schema,
        candidate_tables,
        config.knowledge_index_path,
        semantic_ir,
        state=state,
    )
    memory_block = format_memory_block(filtered_memories, config.knowledge_index_path, state=state)
    feedback_examples = await asyncio.to_thread(
        feedback_repository.search_gold,
        question,
        config.prompt_feedback_examples,
        current_schema_fingerprint=schema_fingerprint(live_schema),
    )
    feedback_block = format_feedback_examples_block(feedback_examples)
    negative_examples = await asyncio.to_thread(feedback_repository.search_negative, question, 2)
    negative_block = format_negative_examples_block(negative_examples)

    prompt_parts = [
        render_semantic_ir(semantic_ir),
        schema_block,
        memory_block,
        feedback_block,
        negative_block,
        "【生成要求】",
        "1. 必须使用 SQL Server 语法。",
        "2. 只能使用上面出现过的真实表名和字段名。",
        f"3. 如果无法从约束中确定字段或关联关系，不要猜测，直接返回 {REFUSAL_TOKEN}。",
        "4. 语义中间表示是硬约束；指标、维度、实体值、日期范围和结果粒度不得遗漏。",
    ]
    prompt_parts.extend(render_required_constraint_blocks(constraints))
    prompt = "\n\n".join(part for part in prompt_parts if part)

    return {
        "prompt": prompt,
        "candidate_tables": candidate_tables,
        "candidate_scores": scores,
        "candidate_score_reasons": score_reasons,
        "live_schema": live_schema,
        "filtered_memories": filtered_memories,
        "feedback_examples": feedback_examples,
        "negative_examples": negative_examples,
        "required_constraints": constraints,
        "required_filters": constraints.get("filters", []),
        "required_tables": constraints.get("tables", []),
        "required_joins": constraints.get("joins", []),
        "semantic_ir": semantic_ir,
        "semantic_ir_dict": semantic_ir.to_dict(),
        "insufficient_context": insufficient_context,
        "insufficiency_reason": insufficiency_reason,
    }


def validate_sql(
    sql: str,
    live_schema: dict[str, dict[str, Any]],
    question: str = "",
    semantic_ir: QuestionSemanticIR | None = None,
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
