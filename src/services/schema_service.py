"""
整体职责
1）读取实时 Schema
2）检索训练记忆
3）筛选候选表并构造 prompt
4）在生成后做本地 SQL 校验

定位
- 相比 training.py ，它不生产知识，而是消费训练产物和实时数据库结构。
- 相比 sql_service.py ，它不直接调模型，而是为模型生成前提供约束，为生成后提供校验。
- 所以它本质上是 Text2SQL 的“运行时约束层 + 拒答层 + 校验层”。

一句话总结
- schema_service.py 决定“模型应该怎么被约束”；
- sql_service.py 决定“模型怎么生成、怎么校验、怎么执行、怎么回流学习”。

"""

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from vanna import ToolContext
from vanna.capabilities.sql_runner.models import RunSqlToolArgs
from vanna.core.user.models import User

from ..core.config import settings
from ..core.logging import setup_logging
from .ast_validator import validate_tsql_ast
from .feedback_store import search_gold_examples, search_negative_examples
from .semantic_ir import QuestionSemanticIR, parse_question_semantics, render_semantic_ir

logger = setup_logging("text2sql.schema_service")

# 定义拒答阈值和安全边界
REFUSAL_TOKEN = "INSUFFICIENT_SCHEMA_CONTEXT"
MIN_CONFIDENT_TABLE_SCORE = 6
MIN_CANDIDATE_TABLES = 1

# 定义哪些训练记忆更值得进入 prompt，以及它们对候选表打分的权重
STRUCTURE_ONLY_SOURCE_TYPES = {"table_description", "column_schema", "foreign_key"}
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
MEMORY_TYPE_WEIGHTS = {
    "feedback_example": 36,
    "question_sql_example": 30,
    "structured_question": 26,
    "analogy_rule": 25,
    "join_path": 24,
    "question_template": 18,
    "metric_rule": 16,
    "time_rule": 14,
    "synonym_rule": 12,
    "alias_dict": 12,
    "field_alias": 12,
    "table_alias": 10,
    "sample_values": 10,
    "column_profile": 8,
    "table_role": 8,
}
MAX_SCHEMA_COLUMNS_PER_TABLE = 12
MAX_MEMORY_BLOCK_ITEMS = 8
MAX_MEMORY_LINE_LENGTH = 320

# 直接从 SQL Server 读取表、字段、外键的实时元数据查询
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

# 运行时缓存，避免每次请求都重复读数据库和索引文件
_live_schema_cache: Optional[dict[str, dict[str, Any]]] = None
_live_schema_cached_at: float = 0.0
_live_schema_lock = asyncio.Lock()
_knowledge_index_cache: Optional[list[dict[str, Any]]] = None

# 基础文本归一化，统一空白和大小写，便于后续匹配
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# 在 _normalize() 基础上进一步去掉常见中英文标点，提升问题和训练文本匹配的鲁棒性
def _normalize_match_text(text: str) -> str:
    text = _normalize(text)
    text = re.sub(r"[?？。！!；;，,]+", "", text)
    return text.strip()


# 从 knowledge_index.json 读取 sidecar 索引，得到每条训练知识的类型、表名、字段名、标签、优先级等元数据
def _load_knowledge_index() -> list[dict[str, Any]]:
    global _knowledge_index_cache
    if _knowledge_index_cache is not None:
        return _knowledge_index_cache

    index_path = Path(settings.knowledge_index_path)
    if not index_path.exists():
        _knowledge_index_cache = []
        return _knowledge_index_cache

    try:
        _knowledge_index_cache = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load knowledge index: %s", exc)
        _knowledge_index_cache = []
    return _knowledge_index_cache


# 清空实时 Schema 和知识索引缓存，通常用于训练后或结构变化后强制刷新。
def reset_schema_cache() -> None:
    global _live_schema_cache, _live_schema_cached_at, _knowledge_index_cache
    _live_schema_cache = None
    _live_schema_cached_at = 0.0
    _knowledge_index_cache = None


# 判断当前 schema 缓存是否仍在 TTL 有效期内。
def _schema_cache_valid() -> bool:
    if _live_schema_cache is None:
        return False
    if settings.schema_cache_ttl_seconds <= 0:
        return True
    return (time.monotonic() - _live_schema_cached_at) < settings.schema_cache_ttl_seconds

# 封装底层 sql_runner 调用，执行元数据 SQL 查询。
async def _run_sql(sql: str) -> Any:
    from ..core.agent import get_sql_runner

    sql_runner = get_sql_runner()
    args = RunSqlToolArgs(sql=sql)
    return await sql_runner.run_sql(args, None)


# 读取实时数据库中的表描述、字段定义和外键关系，并整理成统一结构，是整个运行时结构约束的事实来源。
# 缓存带 TTL（SCHEMA_CACHE_TTL_SECONDS），过期自动刷新；并发请求下只有一个协程真正查库。
async def get_live_schema(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    global _live_schema_cache, _live_schema_cached_at
    if not force_refresh and _schema_cache_valid():
        return _live_schema_cache

    async with _live_schema_lock:
        if not force_refresh and _schema_cache_valid():
            return _live_schema_cache
        return await _load_live_schema()


async def _load_live_schema() -> dict[str, dict[str, Any]]:
    global _live_schema_cache, _live_schema_cached_at
    schema: dict[str, dict[str, Any]] = {}

    table_df = await _run_sql(LIVE_TABLE_QUERY)
    for _, row in table_df.iterrows():
        table_name = str(row["TABLE_NAME"])
        schema[table_name] = {
            "description": str(row.get("TABLE_DESCRIPTION") or ""),
            "columns": {},
            "foreign_keys": [],
        }

    column_df = await _run_sql(LIVE_SCHEMA_QUERY)
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

    fk_df = await _run_sql(LIVE_FK_QUERY)
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

    _live_schema_cache = schema
    _live_schema_cached_at = time.monotonic()
    logger.info("Loaded live schema for %d tables", len(schema))
    return schema


# 从知识记忆中按问题检索相关文本，用于后续候选表打分和 prompt 规则补充。
async def retrieve_memories(question: str, limit: int = 20) -> list[str]:
    from ..core.agent import get_knowledge_memory

    knowledge_memory = get_knowledge_memory()
    context = ToolContext(
        user=User(id="query", username="query"),
        conversation_id="schema-service",
        request_id=str(uuid.uuid4()),
        agent_memory=knowledge_memory,
    )
    results = await knowledge_memory.search_text_memories(
        query=question, context=context, limit=limit
    )
    vector_contents = [item.memory.content for item in results]

    # Hybrid retrieval: merge dense Chroma results with deterministic lexical
    # matches from the metadata sidecar, then rerank by RRF-like rank, source
    # quality and exact business-tag overlap.
    index_entries = _load_knowledge_index()
    keywords = _question_keywords(question)
    scores: dict[str, float] = {}
    for rank, content in enumerate(vector_contents, start=1):
        scores[content] = scores.get(content, 0.0) + 60.0 / (10 + rank)

    for entry in index_entries:
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        searchable = " ".join(
            [
                content,
                *[str(item) for item in entry.get("aliases", []) or []],
                *[str(item) for item in entry.get("field_names", []) or []],
                *[str(item) for item in entry.get("enum_values", []) or []],
                *[str(item) for item in entry.get("metric_tags", []) or []],
                *[str(item) for item in entry.get("dimension_tags", []) or []],
                *[str(item) for item in entry.get("filter_tags", []) or []],
            ]
        ).lower()
        overlap = sum(1 for keyword in keywords if keyword.lower() in searchable)
        if overlap <= 0:
            continue
        priority = int(entry.get("priority", 50) or 50)
        confidence = int(entry.get("confidence", 50) or 50)
        exact_aliases = sum(
            1
            for alias in entry.get("aliases", []) or []
            if _token_in_question(question, str(alias))
        )
        scores[content] = scores.get(content, 0.0) + (
            overlap * 8 + exact_aliases * 10 + priority / 20 + confidence / 25
        )

    ranked = sorted(scores, key=lambda content: (-scores[content], content))
    return ranked[:limit]


# 把问题拆成指标、维度、时间、过滤、角色等标签集合，用于召回和打分。
def _extract_question_tags(question: str) -> dict[str, list[str]]:
    tag_mapping = {
        "metric_tags": ["采集量", "采血量", "人次", "次数", "数量", "金额", "平均"],
        "dimension_tags": [
            "机构",
            "单位",
            "血站",
            "城市",
            "地区",
            "地市",
            "日期",
            "时间",
        ],
        "time_tags": ["今年", "去年", "本月", "上月", "本周", "最近", "年", "月", "日"],
        "filter_tags": ["全血", "成分血", "阴性", "阳性", "男", "女"],
        "role_tags": ["字典", "维度", "明细", "流水", "统计"],
    }
    tags: dict[str, list[str]] = {}
    for key, values in tag_mapping.items():
        tags[key] = [value for value in values if value in question]
    return tags


# 根据字段名、描述、类型，给字段标注“时间维度/维度字段/枚举字段/度量字段”等角色。
def _schema_column_role_tags(
    column_name: str, description: str, data_type: str
) -> list[str]:
    text = f"{column_name} {description} {data_type}".lower()
    tags: list[str] = []
    if any(token in text for token in ["date", "time", "日期", "时间"]):
        tags.append("time_dimension")
    if any(
        token in text
        for token in ["city", "inst", "org", "机构", "城市"]
    ):
        tags.append("dimension")
    if any(
        token in text
        for token in ["type", "status", "flag", "code", "类型", "状态", "标志"]
    ):
        tags.append("enum_candidate")
    if any(
        token in text
        for token in ["volume", "amount", "count", "qty", "量", "次数", "金额"]
    ):
        tags.append("measure")
    return list(dict.fromkeys(tags))


# 规则定义入口
def _build_required_constraint_bundle(
    question: str, semantic_ir: QuestionSemanticIR | None = None
) -> dict[str, list[dict[str, str]]]:
    semantic_ir = semantic_ir or parse_question_semantics(question)
    constraints: dict[str, list[dict[str, str]]] = {
        "filters": [],
        "tables": [],
        "joins": [],
    }

    blood_types = set(semantic_ir.entity_filters.get("blood_type", ()))
    if "0" in blood_types:
        constraints["filters"].append(
            {
                "label": "全血过滤",
                "column": "BCType",
                "value": "0",
                "description": "问题提到“全血”时，SQL 必须包含 BCType = '0' 过滤条件。",
            }
        )
    if "1" in blood_types:
        constraints["filters"].append(
            {
                "label": "成分血过滤",
                "column": "BCType",
                "value": "1",
                "description": "问题提到“成分血/机采”时，SQL 必须包含 BCType = '1' 过滤条件。",
            }
        )

    required_tables = set(semantic_ir.required_tables)
    if "Pub_OrgAddress" in required_tables:
        constraints["tables"].append(
            {
                "table": "Pub_OrgAddress",
                "description": "问题提到“机构/城市/机构名称”等机构主数据时，SQL 必须包含 Pub_OrgAddress 表。",
            }
        )
    if {"Stat_Collection", "Pub_OrgAddress"}.issubset(required_tables):
        constraints["joins"].append(
            {
                "left_table": "Stat_Collection",
                "right_table": "Pub_OrgAddress",
                "left_column": "BTSID",
                "right_column": "InstID",
                "description": "问题涉及机构/城市维度的采集统计时，若使用 Stat_Collection 事实数据，必须按 Stat_Collection.BTSID = Pub_OrgAddress.InstID 关联 Pub_OrgAddress。",
            }
        )
    for city in semantic_ir.entity_filters.get("city", ()):
        constraints["filters"].append(
            {
                "label": "城市过滤",
                "column": "City",
                "value": city,
                "description": f"问题指定城市“{city}”，SQL 必须使用规范实体值 City = '{city}'。",
            }
        )
    return constraints


# 候选表兜底
def _apply_required_candidate_tables(
    live_schema: dict[str, dict[str, Any]],
    candidate_tables: list[str],
    scores: dict[str, int],
    score_reasons: dict[str, list[dict[str, Any]]],
    constraints: dict[str, list[dict[str, str]]],
) -> list[str]:
    required_tables = [
        item["table"]
        for item in constraints.get("tables", [])
        if item["table"] in live_schema
    ]
    for join_rule in constraints.get("joins", []):
        for table_name in [join_rule["left_table"], join_rule["right_table"]]:
            if table_name in live_schema:
                required_tables.append(table_name)

    ordered_tables = list(dict.fromkeys([*candidate_tables, *required_tables]))
    for table_name in required_tables:
        scores[table_name] = max(scores.get(table_name, 0), MIN_CONFIDENT_TABLE_SCORE)
        _add_score_reason(
            score_reasons,
            table_name,
            MIN_CONFIDENT_TABLE_SCORE,
            "required_constraint",
            "问题显式要求该表参与查询",
        )
    return ordered_tables


# Prompt 渲染
def _render_required_constraint_blocks(
    constraints: dict[str, list[dict[str, str]]]
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


# 从问题里抽取中文词块和英文标识词，用于描述重叠打分计算
def _question_keywords(question: str) -> list[str]:
    lowered = question.lower()
    keywords = re.findall(r"[a-zA-Z_]\w+|[\u4e00-\u9fff]{2,}", lowered)
    return list(dict.fromkeys(keywords))


# 判断某个 token 是否真的出现在问题里，兼顾中文包含匹配和英文单词边界匹配
def _token_in_question(question: str, token: str) -> bool:
    question_normalized = _normalize_match_text(question)
    token = _normalize_match_text(token)
    if not token:
        return False
    if re.search(r"[\u4e00-\u9fff]", token):
        return token in question_normalized
    return re.search(rf"\b{re.escape(token)}\b", question_normalized) is not None


# 判断某条知识索引是否和当前问题匹配，并计算额外加分。
def _entry_question_match_bonus(
    entry: dict[str, Any], question: str, question_tags: dict[str, list[str]]
) -> tuple[bool, int]:
    matched = False
    bonus = 0

    for alias in entry.get("aliases", []):
        if _token_in_question(question, str(alias)):
            matched = True
            bonus += 4
    for field_name in entry.get("field_names", []):
        if _token_in_question(question, str(field_name)):
            matched = True
            bonus += 4
    for enum_value in entry.get("enum_values", []):
        if _token_in_question(question, str(enum_value)):
            matched = True
            bonus += 5
    granularity = str(entry.get("granularity") or "").strip()
    if granularity and granularity != "未识别" and granularity in question:
        matched = True
        bonus += 4
    for tag_key, tags in question_tags.items():
        entry_tags = set(entry.get(tag_key, []) or [])
        if tags and entry_tags.intersection(tags):
            matched = True
            bonus += 4

    return matched, bonus


# 记录每张候选表为何得分，给前端和调试用的 candidate_score_reasons 提供解释数据。
def _add_score_reason(
    reasons: dict[str, list[dict[str, Any]]],
    table_name: str,
    score: int,
    reason_type: str,
    detail: str,
) -> None:
    if score <= 0 or not table_name:
        return
    reasons.setdefault(table_name, []).append(
        {"score": score, "type": reason_type, "detail": detail}
    )


# 这是候选表召回核心函数，综合表名命中、字段命中、表描述命中、索引记忆命中、join 提示、描述重叠等因素给每张表打分，并扩展必要的外键关联表。
def select_candidate_tables(
    question: str, live_schema: dict[str, dict[str, Any]], memory_texts: list[str]
) -> tuple[list[str], dict[str, int], dict[str, list[dict[str, Any]]]]:
    index_entries = _load_knowledge_index()
    scores: dict[str, int] = {table_name: 0 for table_name in live_schema}
    reasons: dict[str, list[dict[str, Any]]] = {
        table_name: [] for table_name in live_schema
    }
    question_tags = _extract_question_tags(question)
    question_keywords = _question_keywords(question)

    for table_name, info in live_schema.items():
        if _token_in_question(question, table_name):
            scores[table_name] += 6
            _add_score_reason(reasons, table_name, 6, "table_name", table_name)
        for column_name, column_info in info["columns"].items():
            if _token_in_question(question, column_name):
                scores[table_name] += 4
                _add_score_reason(reasons, table_name, 4, "column_name", column_name)
            description = str(column_info.get("description") or "")
            if description and _token_in_question(question, description):
                scores[table_name] += 2
                _add_score_reason(
                    reasons, table_name, 2, "column_description", description[:80]
                )
        if info.get("description") and _token_in_question(
            question, str(info.get("description"))
        ):
            scores[table_name] += 3
            _add_score_reason(
                reasons,
                table_name,
                3,
                "table_description",
                str(info.get("description"))[:80],
            )

    for entry in index_entries:
        entry_tables = [
            table for table in entry.get("table_names", []) if table in scores
        ]
        if not entry_tables:
            continue
        matched, match_bonus = _entry_question_match_bonus(
            entry, question, question_tags
        )
        if not matched:
            continue
        source_priority = int(entry.get("priority", 50))
        confidence = int(entry.get("confidence", 50))
        source_type = str(entry.get("source_type") or "")
        bonus = (
            5
            + (source_priority // 20)
            + (confidence // 25)
            + (MEMORY_TYPE_WEIGHTS.get(source_type, 0) // 8)
            + match_bonus
        )
        for table_name in entry_tables:
            scores[table_name] += bonus
            _add_score_reason(
                reasons,
                table_name,
                bonus,
                f"index:{source_type or 'unknown'}",
                ",".join(entry.get("aliases", [])[:3])
                or ",".join(entry.get("field_names", [])[:3])
                or ",".join(entry.get("enum_values", [])[:3])
                or "标签命中",
            )
        for join_table in entry.get("join_tables", []):
            if join_table in scores:
                join_bonus = max(2, bonus // 2)
                scores[join_table] += join_bonus
                _add_score_reason(
                    reasons,
                    join_table,
                    join_bonus,
                    "join_hint",
                    f"来自 {','.join(entry_tables[:2]) or source_type}",
                )

    for table_name, info in live_schema.items():
        description = str(info.get("description") or "").lower()
        if not description:
            continue
        overlap = [keyword for keyword in question_keywords if keyword in description]
        if overlap:
            overlap_bonus = min(4, len(overlap))
            scores[table_name] += overlap_bonus
            _add_score_reason(
                reasons,
                table_name,
                overlap_bonus,
                "description_overlap",
                ",".join(overlap[:3]),
            )

    memory_blob = "\n".join(memory_texts)
    for table_name in live_schema:
        if table_name in memory_blob:
            scores[table_name] += 3
            _add_score_reason(reasons, table_name, 3, "memory_text", table_name)

    candidates = [table for table, score in scores.items() if score > 0]
    candidates.sort(key=lambda item: (-scores[item], item))

    expanded: list[str] = []
    for table_name in candidates[:4]:
        if table_name not in expanded:
            expanded.append(table_name)
        for fk in live_schema[table_name].get("foreign_keys", []):
            referenced_table = fk.get("referenced_table")
            if referenced_table in live_schema and referenced_table not in expanded:
                expanded.append(referenced_table)

    compact_reasons: dict[str, list[dict[str, Any]]] = {}
    for table_name, items in reasons.items():
        if not items:
            continue
        items.sort(
            key=lambda item: (
                -int(item["score"]),
                str(item["type"]),
                str(item["detail"]),
            )
        )
        compact_reasons[table_name] = items[:8]

    return expanded[:6], scores, compact_reasons


# 根据候选表数量、最高分、训练记忆是否足够，决定当前问题是否应拒答。
def evaluate_context_sufficiency(
    candidate_tables: list[str], scores: dict[str, int], filtered_memories: list[str]
) -> tuple[bool, str]:
    if len(candidate_tables) < MIN_CANDIDATE_TABLES:
        return True, "问题无法映射到任何真实表结构。"

    max_score = max(
        (scores.get(table_name, 0) for table_name in candidate_tables), default=0
    )
    if max_score < MIN_CONFIDENT_TABLE_SCORE:
        return True, "问题与真实表结构的匹配度过低，无法安全生成 SQL。"

    if not filtered_memories and max_score < (MIN_CONFIDENT_TABLE_SCORE + 2):
        return True, "缺少足够的训练规则或示例支撑当前问题。"

    return False, ""


# 从检索出来的原始记忆里过滤和重排，只保留与候选表和当前问题真正相关的高价值语义规则，主动跳过纯结构型低价值记忆。
# 它避免把所有训练文本都塞进 prompt，减少噪音，提升规则命中率。
def filter_memories(
    memory_texts: list[str],
    candidate_tables: list[str],
    limit: int = 12,
    question: str = "",
) -> list[str]:
    if not memory_texts:
        return []

    index_by_content = {
        _normalize(entry.get("content", "")): entry for entry in _load_knowledge_index()
    }
    filtered: list[tuple[int, str]] = []
    question_tags = _extract_question_tags(question)

    for content in memory_texts:
        normalized = _normalize(content)
        entry = index_by_content.get(normalized)
        if entry:
            source_type = entry.get("source_type", "")
            if source_type in STRUCTURE_ONLY_SOURCE_TYPES:
                continue
            entry_tables = set(entry.get("table_names", []))
            priority = int(entry.get("priority", 50))
            confidence = int(entry.get("confidence", 50))
            score = priority + confidence + MEMORY_TYPE_WEIGHTS.get(source_type, 0)
            if entry_tables.intersection(candidate_tables):
                score += 25
            if set(entry.get("join_tables", [])).intersection(candidate_tables):
                score += 20
            for tag_key, tags in question_tags.items():
                if tags and set(tags).intersection(entry.get(tag_key, [])):
                    score += 12
            if source_type in HIGH_VALUE_MEMORY_TYPES or entry_tables.intersection(
                candidate_tables
            ):
                filtered.append((score, content))
                continue

        if any(table_name in content for table_name in candidate_tables):
            filtered.append((40, content))

    if not filtered:
        fallback = []
        for content in memory_texts[:limit]:
            normalized = _normalize(content)
            entry = index_by_content.get(normalized, {})
            fallback.append(
                (
                    int(entry.get("priority", 30)) + int(entry.get("confidence", 30)),
                    content,
                )
            )
        filtered = fallback

    filtered.sort(key=lambda item: (-item[0], item[1]))
    ordered = [content for _, content in filtered]
    return list(dict.fromkeys(ordered))[:limit]


# 当表字段太多时，只挑最相关的字段放进 prompt，优先保留问题命中的字段、时间/维度/指标相关字段、外键字段和索引里命中的字段。
def _select_schema_columns(
    question: str,
    table_name: str,
    table_info: dict[str, Any],
    candidate_tables: list[str],
    index_entries: list[dict[str, Any]],
) -> list[str]:
    columns = table_info.get("columns", {})
    if not columns:
        return []
    if len(columns) <= MAX_SCHEMA_COLUMNS_PER_TABLE:
        return list(columns.keys())

    question_tags = _extract_question_tags(question)
    selected: list[str] = []

    for column_name, column_info in columns.items():
        description = str(column_info.get("description") or "")
        data_type = str(column_info.get("data_type") or "")
        if _token_in_question(question, column_name):
            selected.append(column_name)
        elif description and _token_in_question(question, description):
            selected.append(column_name)
        elif question_tags.get("time_tags") and (
            "time_dimension"
            in _schema_column_role_tags(column_name, description, data_type)
        ):
            selected.append(column_name)
        elif question_tags.get("dimension_tags") and (
            "dimension" in _schema_column_role_tags(column_name, description, data_type)
        ):
            selected.append(column_name)
        elif question_tags.get("metric_tags") and (
            "measure" in _schema_column_role_tags(column_name, description, data_type)
        ):
            selected.append(column_name)

    for fk in table_info.get("foreign_keys", []):
        referenced_table = str(fk.get("referenced_table") or "")
        if referenced_table in candidate_tables:
            column_name = str(fk.get("column_name") or "")
            if column_name:
                selected.append(column_name)

    for entry in index_entries:
        if table_name not in entry.get("table_names", []):
            continue
        matched, _ = _entry_question_match_bonus(entry, question, question_tags)
        if not matched:
            continue
        selected.extend(
            str(field_name)
            for field_name in entry.get("field_names", []) or []
            if field_name in columns
        )

    fallback_ranked = sorted(
        columns.items(),
        key=lambda item: (
            -(
                3
                if "time_dimension"
                in _schema_column_role_tags(
                    item[0],
                    str(item[1].get("description") or ""),
                    str(item[1].get("data_type") or ""),
                )
                else 0
            )
            - (
                2
                if "dimension"
                in _schema_column_role_tags(
                    item[0],
                    str(item[1].get("description") or ""),
                    str(item[1].get("data_type") or ""),
                )
                else 0
            )
            - (
                2
                if "measure"
                in _schema_column_role_tags(
                    item[0],
                    str(item[1].get("description") or ""),
                    str(item[1].get("data_type") or ""),
                )
                else 0
            ),
            item[0],
        ),
    )

    ordered = list(dict.fromkeys(selected))
    for column_name, _ in fallback_ranked:
        if column_name not in ordered:
            ordered.append(column_name)
        if len(ordered) >= MAX_SCHEMA_COLUMNS_PER_TABLE:
            break
    return ordered[:MAX_SCHEMA_COLUMNS_PER_TABLE]


# 把候选表、选中字段、字段说明、外键关系整理成给 LLM 看的“实时数据库结构约束”文本块。
def format_schema_block(
    question: str,
    live_schema: dict[str, dict[str, Any]],
    candidate_tables: list[str],
) -> str:
    if not candidate_tables:
        return ""

    lines = [
        "【实时数据库结构约束】",
        "只允许使用下列真实存在的表和字段；如果信息不足，禁止猜测不存在的表名或字段名。",
    ]
    index_entries = _load_knowledge_index()

    for table_name in candidate_tables:
        table_info = live_schema.get(table_name)
        if not table_info:
            continue
        table_desc = table_info.get("description", "")
        selected_columns = _select_schema_columns(
            question, table_name, table_info, candidate_tables, index_entries
        )
        lines.append(
            f"表: {table_name}" + (f" | 描述: {table_desc}" if table_desc else "")
        )
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
        for fk in table_info.get("foreign_keys", []):
            lines.append(
                f"  - 外键: {table_name}.{fk['column_name']} -> {fk['referenced_table']}"
            )

    return "\n".join(lines)


# 把已验证问答对渲染成 prompt 文本块。
def format_feedback_examples_block(examples: list[dict[str, str]]) -> str:
    if not examples:
        return ""
    lines = ["【已验证问答示例】", "以下示例经过执行或人工验证，优先参考其表选择、关联方式和过滤写法。"]
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


# 把筛选后的训练知识整理成“训练知识与业务规则”文本块，附带来源类型并限制长度。
def format_memory_block(memory_texts: list[str]) -> str:
    if not memory_texts:
        return ""
    index_by_content = {
        _normalize(entry.get("content", "")): entry for entry in _load_knowledge_index()
    }
    lines = ["【训练知识与业务规则】"]
    for content in memory_texts[:MAX_MEMORY_BLOCK_ITEMS]:
        entry = index_by_content.get(_normalize(content), {})
        source_type = str(entry.get("source_type") or "").strip()
        compact = re.sub(r"\s+", " ", content).strip()
        if len(compact) > MAX_MEMORY_LINE_LENGTH:
            compact = compact[: MAX_MEMORY_LINE_LENGTH - 3].rstrip() + "..."
        prefix = f"[{source_type}] " if source_type else ""
        lines.append(f"- {prefix}{compact}")
    omitted_count = max(0, len(memory_texts) - MAX_MEMORY_BLOCK_ITEMS)
    if omitted_count:
        lines.append(f"- 其余训练记忆省略: {omitted_count} 条")
    return "\n".join(lines)


# 这是生成前主入口，串起“取实时 schema -> 检索记忆 -> 候选表打分 -> 过滤记忆 -> 上下文充足性判断 -> 加入 BCType 等显式过滤约束 -> 输出 prompt/context”。
async def build_prompt_context(question: str) -> dict[str, Any]:
    semantic_ir = parse_question_semantics(question)
    live_schema = await get_live_schema()
    memory_texts = await retrieve_memories(question, limit=24)
    candidate_tables, scores, score_reasons = select_candidate_tables(
        question, live_schema, memory_texts
    )
    constraints = _build_required_constraint_bundle(question, semantic_ir)
    candidate_tables = _apply_required_candidate_tables(
        live_schema, candidate_tables, scores, score_reasons, constraints
    )
    filtered_memories = filter_memories(
        memory_texts, candidate_tables, question=question
    )
    insufficient_context, insufficiency_reason = evaluate_context_sufficiency(
        candidate_tables, scores, filtered_memories
    )
    schema_block = format_schema_block(question, live_schema, candidate_tables)
    memory_block = format_memory_block(filtered_memories)
    feedback_examples = await asyncio.to_thread(
        search_gold_examples,
        question,
        settings.prompt_feedback_examples,
    )
    feedback_block = format_feedback_examples_block(feedback_examples)
    negative_examples = await asyncio.to_thread(
        search_negative_examples, question, 2
    )
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
    prompt_parts.extend(_render_required_constraint_blocks(constraints))
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


# 这是生成后主入口，负责本地安全校验。
# 是否识别出表、是否使用了不存在的表、别名解析后字段是否真实存在、单表场景下是否出现未知标识符、是否满足问题要求的必需过滤条件（如 BCType ）。
# 这个函数的核心价值是把“训练里学到的规则”变成“运行时必须满足的规则”，防止 SQL 虽能执行但业务语义错误。
def validate_sql(
    sql: str,
    live_schema: dict[str, dict[str, Any]],
    question: str = "",
    semantic_ir: QuestionSemanticIR | None = None,
) -> Optional[str]:
    semantic_ir = semantic_ir or parse_question_semantics(question)
    return validate_tsql_ast(sql, live_schema, semantic_ir)
