# src/training/pipeline.py
"""
增强版知识训练模块
支持：
- 表/列描述（原有）
- 样本数据采样（新增）
- 业务文档从文件加载（新增）
- 表关系（外键）注入（可选扩展）
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Optional

from vanna import ToolContext, User
from vanna.capabilities.sql_runner.models import RunSqlToolArgs

from ..infrastructure.runtime import (
    get_sql_runner,
    get_knowledge_memory,
    get_agent_memory,
)
from ..core.config import settings
from ..core.logging import setup_logging
from ..application.context_service import get_live_schema, reset_schema_cache
from ..infrastructure.feedback_repository import load_gold_examples
from ..knowledge import KnowledgeBundle, load_knowledge_bundle
from ..evaluation import load_evaluation_cases
from ..domain.semantic_ir import parse_question_semantics

logger = setup_logging("text2sql.training")
TRAINING_FINGERPRINT_VERSION = 5
STRUCTURE_ONLY_SOURCE_TYPES = {"table_description", "column_schema", "foreign_key"}

TRAINING_PRIORITY = {
    "feedback_example": 100,
    "question_sql_example": 95,
    "structured_question": 92,
    "analogy_rule": 91,
    "join_path": 90,
    "question_template": 89,
    "table_role": 88,
    "metric_rule": 86,
    "time_rule": 84,
    "synonym_rule": 82,
    "alias_dict": 80,
    "field_alias": 78,
    "table_alias": 76,
    "column_schema": 72,
    "table_description": 70,
    "foreign_key": 68,
    "column_profile": 66,
    "sample_values": 64,
    "table_card": 90,
    "domain_policy": 88,
    "dimension_rule": 86,
    "refusal_example": 90,
    "negative_sql_example": 90,
}

"""领域同义词规则"""

"""指标规则"""

"""时间表达式规则"""
TIME_EXPRESSION_RULES = [
    {
        "phrase": "今年",
        "aliases": ["本年", "当年", "今年度"],
        "hint": "通常表示从当年 1 月 1 日到下一年 1 月 1 日的时间范围。",
    },
    {
        "phrase": "去年",
        "aliases": ["上年", "上一年", "去年度"],
        "hint": "通常表示上一自然年的完整时间范围。",
    },
    {
        "phrase": "近一年",
        "aliases": ["最近一年", "过去一年", "近12个月"],
        "hint": "通常表示从当前日期向前回溯 1 年的滚动区间。",
    },
    {
        "phrase": "本月",
        "aliases": ["这个月", "当月"],
        "hint": "通常表示当月 1 日到下月 1 日的时间范围。",
    },
    {
        "phrase": "本季度",
        "aliases": ["本季", "当季", "这个季度"],
        "hint": "通常表示当前季度起始日到下一季度起始日。",
    },
]


def _build_index_record(
    text: str,
    source_type: str,
    table_names: Optional[list[str]] = None,
    field_names: Optional[list[str]] = None,
    aliases: Optional[list[str]] = None,
    metric_tags: Optional[list[str]] = None,
    dimension_tags: Optional[list[str]] = None,
    time_tags: Optional[list[str]] = None,
    filter_tags: Optional[list[str]] = None,
    join_tables: Optional[list[str]] = None,
    role_tags: Optional[list[str]] = None,
    profile_tags: Optional[list[str]] = None,
    enum_values: Optional[list[str]] = None,
    granularity: Optional[str] = None,
    confidence: int = 50,
) -> dict[str, Any]:
    return {
        "content": text.strip(),
        "source_type": source_type,
        "table_names": _dedupe_keep_order(table_names or []),
        "field_names": _dedupe_keep_order(field_names or []),
        "aliases": _dedupe_keep_order(aliases or []),
        "metric_tags": _dedupe_keep_order(metric_tags or []),
        "dimension_tags": _dedupe_keep_order(dimension_tags or []),
        "time_tags": _dedupe_keep_order(time_tags or []),
        "filter_tags": _dedupe_keep_order(filter_tags or []),
        "join_tables": _dedupe_keep_order(join_tables or []),
        "role_tags": _dedupe_keep_order(role_tags or []),
        "profile_tags": _dedupe_keep_order(profile_tags or []),
        "enum_values": _dedupe_keep_order(enum_values or []),
        "granularity": (granularity or "").strip(),
        "priority": TRAINING_PRIORITY.get(source_type, 50),
        "confidence": confidence,
    }


def _flush_knowledge_index(records: list[dict[str, Any]]) -> None:
    index_path = Path(settings.knowledge_index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("知识索引已写入: %s (%d 条)", index_path, len(records))


def _append_index_record(
    index_records: list[dict[str, Any]],
    text: str,
    *,
    source_type: str,
    table_names: Optional[list[str]] = None,
    field_names: Optional[list[str]] = None,
    aliases: Optional[list[str]] = None,
    metric_tags: Optional[list[str]] = None,
    dimension_tags: Optional[list[str]] = None,
    time_tags: Optional[list[str]] = None,
    filter_tags: Optional[list[str]] = None,
    join_tables: Optional[list[str]] = None,
    role_tags: Optional[list[str]] = None,
    profile_tags: Optional[list[str]] = None,
    enum_values: Optional[list[str]] = None,
    granularity: Optional[str] = None,
    confidence: int = 50,
) -> None:
    content = text.strip()
    if not content:
        return
    index_records.append(
        _build_index_record(
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


async def _save_training_text(
    knowledge_memory,
    text: str,
    ctx,
    index_records: list[dict[str, Any]],
    source_type: str,
    table_names: Optional[list[str]] = None,
    field_names: Optional[list[str]] = None,
    aliases: Optional[list[str]] = None,
    metric_tags: Optional[list[str]] = None,
    dimension_tags: Optional[list[str]] = None,
    time_tags: Optional[list[str]] = None,
    filter_tags: Optional[list[str]] = None,
    join_tables: Optional[list[str]] = None,
    role_tags: Optional[list[str]] = None,
    profile_tags: Optional[list[str]] = None,
    enum_values: Optional[list[str]] = None,
    granularity: Optional[str] = None,
    confidence: int = 50,
) -> None:
    content = text.strip()
    if not content:
        return

    await knowledge_memory.save_text_memory(content, ctx)
    _append_index_record(
        index_records,
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


def _dedupe_keep_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _first_sentence(text: str) -> str:
    return re.split(r"[。；;，,]", text.strip(), maxsplit=1)[0].strip()










def _field_aliases(description: str) -> list[str]:
    core = _first_sentence(description)
    aliases = [core]

    if core.endswith("名称"):
        aliases.append(core[:-2] + "名")
    if core.endswith("编号"):
        aliases.append(core[:-2] + "ID")
    if core.endswith("日期"):
        aliases.append(core[:-2] + "时间")
    if core.startswith("是否"):
        aliases.append(core[2:])

    replacements = {
        "采集": "采血",
        "机构": "单位",
        "血液": "献血",
    }
    for old, new in replacements.items():
        if old in core:
            aliases.append(core.replace(old, new))

    return _dedupe_keep_order(aliases)












































def _extract_dimensions(question: str, sql: str) -> list[str]:
    dimensions: list[str] = []
    mapping = {
        "机构": ["机构", "单位", "血站", "OrgName", "InstID", "BTSName", "BTSID"],
        "城市": ["城市", "地区", "地市", "City"],
        "日期": ["日期", "时间", "BCDate"],
        "血型": ["ABO", "RhD", "血型"],
        "性别": ["性别", "Sex"],
    }
    combined = f"{question}\n{sql}"
    for dimension, keywords in mapping.items():
        if any(keyword in combined for keyword in keywords):
            dimensions.append(dimension)
    return _dedupe_keep_order(dimensions)


def _extract_metric(question: str, sql: str) -> str:
    if "COUNT(" in sql.upper() or "人次" in question or "次数" in question:
        return "采集人次"
    if "SUM(" in sql.upper() or "采集量" in question or "采血量" in question:
        return "采集量"
    if "AVG(" in sql.upper() or "平均" in question:
        return "平均值"
    return "未识别"


def _extract_time_expression(question: str, sql: str) -> str:
    for rule in TIME_EXPRESSION_RULES:
        phrases = [rule["phrase"], *rule["aliases"]]
        if any(phrase in question for phrase in phrases):
            return str(rule["phrase"])

    year_match = re.search(r"(19|20)\d{2}年", question)
    if year_match:
        return year_match.group(0)

    range_match = re.findall(r"'(\d{4}-\d{2}-\d{2})'", sql)
    if len(range_match) >= 2:
        return f"{range_match[0]} ~ {range_match[1]}"

    return "未显式说明"


def _extract_filters(question: str, sql: str) -> list[str]:
    filters: list[str] = []
    mapping = {
        "全血": ["全血", "BCType = '0'", "BCType='0'", "BCType = 0", "BCType=0"],
        "成分血": [
            "成分血",
            "机采",
            "BCType = '1'",
            "BCType='1'",
            "BCType = 1",
            "BCType=1",
        ],
        "Rh阴性": ["阴性", "Rh阴性"],
        "Rh阳性": ["阳性", "Rh阳性"],
        "男性": ["男", "男性"],
        "女性": ["女", "女性"],
    }
    combined = f"{question}\n{sql}"
    for label, keywords in mapping.items():
        if any(keyword in combined for keyword in keywords):
            filters.append(label)
    return _dedupe_keep_order(filters)


def _question_sql_metadata(question: str, sql: str) -> dict[str, Any]:
    dimensions = _extract_dimensions(question, sql)
    metric = _extract_metric(question, sql)
    time_expression = _extract_time_expression(question, sql)
    filters = _extract_filters(question, sql)
    sql_tables = [
        table for table in _extract_sql_table_names(sql) if _should_include_table(table)
    ]
    return {
        "dimensions": dimensions,
        "metric": metric,
        "time_expression": time_expression,
        "filters": filters,
        "sql_tables": sql_tables,
    }










def _normalize_training_question(question: str) -> str:
    question = re.sub(r"\s+", " ", str(question or "")).strip()
    question = re.sub(r"^[0-9]+[\.\-、:：\s]+", "", question)
    question = re.sub(r"[?？。！!；;]+$", "", question).strip()
    return question


def _question_aliases(question: str) -> list[str]:
    normalized = _normalize_training_question(question)
    aliases = [str(question or "").strip(), normalized]
    if normalized:
        aliases.append(f"{normalized}?")
        aliases.append(f"{normalized}？")
    return _dedupe_keep_order([alias for alias in aliases if alias])




def _extract_sql_table_names(sql: str) -> list[str]:
    matches = re.findall(
        r"(?is)\b(?:from|join|update|into)\s+((?:\[[^\]]+\]|\w+)(?:\.(?:\[[^\]]+\]|\w+))*)",
        sql,
    )
    table_names: list[str] = []
    for match in matches:
        parts = [part.strip("[]") for part in match.split(".")]
        if parts:
            table_names.append(parts[-1])
    return _dedupe_keep_order(table_names)




def _metric_tags_from_metadata(metadata: dict[str, Any]) -> Optional[list[str]]:
    metric = str(metadata.get("metric") or "").strip()
    return [metric] if metric and metric != "未识别" else None


def _time_tags_from_metadata(metadata: dict[str, Any]) -> Optional[list[str]]:
    time_expression = str(metadata.get("time_expression") or "").strip()
    return (
        [time_expression]
        if time_expression and time_expression != "未显式说明"
        else None
    )




async def _train_question_sql_examples(
    knowledge_memory,
    ctx,
    question_pairs: list[tuple[str, str]],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    report["question_sql_examples_trained"] = len(question_pairs)
    for question, sql in question_pairs:
        metadata = _question_sql_metadata(question, sql)
        example_text = f"中文问题与 SQL 示例:\n问题: {question}\nSQL Server SQL:\n{sql}"
        await _save_training_text(
            knowledge_memory,
            example_text,
            ctx,
            index_records,
            source_type="question_sql_example",
            table_names=metadata["sql_tables"],
            aliases=_question_aliases(question),
            metric_tags=_metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=_time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            join_tables=metadata["sql_tables"],
            confidence=95,
        )






def _configured_training_tables() -> Optional[set[str]]:
    if not settings.training_tables:
        return None
    return {
        item.strip() for item in settings.training_tables.split(",") if item.strip()
    }


def _should_include_table(table_name: str) -> bool:
    allowed = _configured_training_tables()
    if not allowed:
        return True
    return table_name in allowed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_file(path_str: str, payload: Any) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json_file(path_str: str) -> Optional[dict[str, Any]]:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 JSON 文件失败 %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path_str: Optional[str]) -> str:
    if not path_str:
        return ""
    path = Path(path_str)
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_directory(path_str: str) -> str:
    root = Path(path_str)
    if not root.exists():
        return ""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _hash_live_schema(live_schema: dict[str, dict[str, Any]]) -> str:
    serialized = json.dumps(live_schema, ensure_ascii=False, sort_keys=True)
    return _hash_text(serialized)


def _build_training_fingerprint(
    *,
    include_samples: bool,
    sample_rows: int,
    live_schema_hash: str,
) -> dict[str, Any]:
    return {
        "version": TRAINING_FINGERPRINT_VERSION,
        "include_samples": bool(include_samples),
        "sample_rows": int(sample_rows),
        "structured_knowledge_dir": settings.structured_knowledge_dir,
        "structured_knowledge_hash": _hash_directory(
            settings.structured_knowledge_dir
        ),
        "live_schema_hash": live_schema_hash,
        "retrieval_train_set_hash": _hash_file(settings.retrieval_train_set_path),
        "eval_dev_set_hash": _hash_file(settings.eval_dev_set_path),
        "eval_test_set_hash": _hash_file(settings.eval_test_set_path),
        "training_eval_split": settings.training_eval_split,
        "feedback_examples_hash": _hash_file(settings.feedback_examples_path),
        "table_retrieval_calibrator_hash": _hash_file(
            settings.table_retrieval_calibrator_path
        ),
        "training_tables": sorted(_configured_training_tables() or []),
        "sample_tables": sorted(
            item.strip()
            for item in (settings.sample_tables or "").split(",")
            if item.strip()
        ),
        "profiling_max_tables": settings.profiling_max_tables,
        "profiling_max_columns_per_table": settings.profiling_max_columns_per_table,
        "profiling_max_distinct_values": settings.profiling_max_distinct_values,
        "feedback_min_result_rows": settings.feedback_min_result_rows,
        "feedback_require_nonempty_result": settings.feedback_require_nonempty_result,
        "feedback_require_execution_success": settings.feedback_require_execution_success,
        "priority_hash": _hash_text(json.dumps(TRAINING_PRIORITY, sort_keys=True)),
    }


def _should_skip_training(fingerprint: dict[str, Any]) -> bool:
    if not settings.training_skip_unchanged:
        return False
    state = _read_json_file(settings.training_state_path)
    if not state:
        return False
    artifacts = [
        settings.training_manifest_path,
        settings.training_report_path,
        settings.knowledge_index_path,
    ]
    if not all(Path(path).exists() for path in artifacts):
        return False
    return state.get("fingerprint") == fingerprint


def _write_training_state(fingerprint: dict[str, Any], report: dict[str, Any]) -> None:
    payload = {
        "updated_at": _now_iso(),
        "fingerprint": fingerprint,
        "summary": {
            "table_count": report.get("table_count", 0),
            "column_count": report.get("column_count", 0),
            "knowledge_records": report.get("knowledge_records", 0),
            "memory_records": report.get("memory_records", 0),
            "evaluation_summary": report.get("evaluation_summary", {}),
        },
    }
    _write_json_file(settings.training_state_path, payload)


def _empty_training_report(include_samples: bool, sample_rows: int) -> dict[str, Any]:
    return {
        "started_at": _now_iso(),
        "finished_at": None,
        "include_samples": include_samples,
        "sample_rows": sample_rows,
        "skipped": False,
        "skip_reason": None,
        "evaluation_split": settings.training_eval_split,
        "table_count": 0,
        "column_count": 0,
        "knowledge_records": 0,
        "memory_records": 0,
        "knowledge_records_by_type": {},
        "memory_records_by_type": {},
        "structure_records": 0,
        "deduped_records_removed": 0,
        "knowledge_source": "structured",
        "structured_files": 0,
        "structured_records": 0,
        "structured_records_rejected": 0,
        "question_sql_examples": 0,
        "question_sql_examples_trained": 0,
        "feedback_examples": 0,
        "feedback_examples_rejected": 0,
        "feedback_examples_loaded": 0,
        "profiling_tables_considered": 0,
        "profiling_columns_selected": 0,
        "evaluations": [],
        "warnings": [],
    }


def _finalize_training_report(
    report: dict[str, Any], index_records: list[dict[str, Any]]
) -> dict[str, Any]:
    counts = Counter(record.get("source_type", "unknown") for record in index_records)
    memory_counts = Counter(
        record.get("source_type", "unknown")
        for record in index_records
        if record.get("source_type") not in STRUCTURE_ONLY_SOURCE_TYPES
    )
    report["finished_at"] = _now_iso()
    report["knowledge_records"] = len(index_records)
    report["knowledge_records_by_type"] = dict(sorted(counts.items()))
    report["memory_records"] = sum(memory_counts.values())
    report["memory_records_by_type"] = dict(sorted(memory_counts.items()))
    report["structure_records"] = report["knowledge_records"] - report["memory_records"]
    return report


def _merge_index_values(existing: list[str], incoming: list[str]) -> list[str]:
    return _dedupe_keep_order([*(existing or []), *(incoming or [])])


def _dedupe_index_records(
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
            existing[field] = _merge_index_values(
                list(existing.get(field, []) or []),
                list(record.get(field, []) or []),
            )
        existing["priority"] = max(
            int(existing.get("priority", 0) or 0),
            int(record.get("priority", 0) or 0),
        )
        existing["confidence"] = max(
            int(existing.get("confidence", 0) or 0),
            int(record.get("confidence", 0) or 0),
        )
        if not existing.get("granularity") and record.get("granularity"):
            existing["granularity"] = record.get("granularity")

    deduped_records = [deduped[key] for key in ordered_keys]
    return deduped_records, max(0, len(records) - len(deduped_records))


def _write_training_manifest(
    *,
    include_samples: bool,
    sample_rows: int,
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    fingerprint: dict[str, Any],
) -> None:
    manifest = {
        "generated_at": _now_iso(),
        "include_samples": include_samples,
        "sample_rows": sample_rows,
        "training_tables": sorted(_configured_training_tables() or []),
        "record_count": len(index_records),
        "record_types": sorted(
            {record.get("source_type", "") for record in index_records}
        ),
        "table_names": sorted(
            {
                table_name
                for record in index_records
                for table_name in record.get("table_names", [])
                if table_name
            }
        ),
        "inputs": fingerprint,
        "summary": {
            "table_count": report.get("table_count", 0),
            "column_count": report.get("column_count", 0),
            "memory_records": report.get("memory_records", 0),
            "structure_records": report.get("structure_records", 0),
            "question_sql_examples": report.get("question_sql_examples", 0),
            "question_sql_examples_trained": report.get(
                "question_sql_examples_trained", 0
            ),
            "feedback_examples": report.get("feedback_examples", 0),
            "deduped_records_removed": report.get("deduped_records_removed", 0),
        },
    }
    _write_json_file(settings.training_manifest_path, manifest)


def _column_role_tags(field_name: str, description: str, data_type: str) -> list[str]:
    text = f"{field_name} {description} {data_type}".lower()
    tags: list[str] = []
    if any(token in text for token in ["date", "time", "日期", "时间"]):
        tags.append("time_dimension")
    if any(token in text for token in ["city", "inst", "org", "机构", "城市"]):
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
    return _dedupe_keep_order(tags)


def _infer_table_role(table_name: str, info: dict[str, Any]) -> tuple[list[str], str]:
    text = f"{table_name} {info.get('description', '')}".lower()
    columns = info.get("columns", {})
    column_names = [name.lower() for name in columns.keys()]
    roles: list[str] = []
    granularity = "未识别"
    if any(token in text for token in ["dict", "dictionary", "字典"]):
        roles.append("dictionary")
    if any(token in text for token in ["stat", "fact", "记录", "流水", "采集"]):
        roles.append("fact")
    if any(token in text for token in ["pub_", "dim", "master", "主数据", "维度"]):
        roles.append("dimension")
    if not roles:
        roles.append("general")

    if any(
        token in column_names for token in ["instid", "orgname", "btsid", "btsname"]
    ):
        granularity = "机构"
    elif any(token in column_names for token in ["city", "district"]):
        granularity = "地区"
    elif any("date" in token or "time" in token for token in column_names):
        granularity = "时间"
    return _dedupe_keep_order(roles), granularity


def _build_table_schema_index_records(
    live_schema: dict[str, dict[str, Any]],
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    table_count = 0
    column_count = 0
    for table_name, info in sorted(live_schema.items()):
        if not _should_include_table(table_name):
            continue
        description = str(info.get("description") or "")
        roles, granularity = _infer_table_role(table_name, info)
        columns = info.get("columns", {})
        field_names = list(columns.keys())
        dimension_tags = [
            name
            for name, col in columns.items()
            if "dimension"
            in _column_role_tags(
                name,
                str(col.get("description", "")),
                str(col.get("data_type", "")),
            )
        ]
        metric_tags = [
            name
            for name, col in columns.items()
            if "measure"
            in _column_role_tags(
                name,
                str(col.get("description", "")),
                str(col.get("data_type", "")),
            )
        ]
        aliases = [description] if description else None
        _append_index_record(
            index_records,
            f"表: {table_name} 描述: {description}",
            source_type="table_description",
            table_names=[table_name],
            aliases=aliases,
            role_tags=roles,
            granularity=granularity,
            confidence=72,
        )
        _append_index_record(
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
            aliases=_dedupe_keep_order(
                alias
                for column_info in columns.values()
                for alias in _field_aliases(str(column_info.get("description", "")))
                if column_info.get("description")
            ),
            role_tags=roles,
            dimension_tags=dimension_tags,
            metric_tags=metric_tags,
            granularity=granularity,
            confidence=82,
        )
        for fk in info.get("foreign_keys", []):
            referenced_table = str(fk.get("referenced_table") or "")
            if not referenced_table or not _should_include_table(referenced_table):
                continue
            column_name = str(fk.get("column_name") or "")
            _append_index_record(
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


def _normalize_assertion_text(text: str) -> str:
    lowered = text.lower()
    lowered = lowered.replace("[", "").replace("]", "")
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _normalized_contains(sql: str, expected: str) -> bool:
    normalized_sql = _normalize_assertion_text(sql)
    normalized_expected = _normalize_assertion_text(expected)
    if not normalized_expected:
        return True
    join_match = re.match(
        r"^(?:[\w\[\]\.]+)\.([\w\[\]]+)\s*=\s*(?:[\w\[\]\.]+)\.([\w\[\]]+)$",
        normalized_expected,
    )
    if join_match:
        actual_join_pairs = {
            tuple(
                sorted(
                    [
                        left.split(".")[-1].strip("[]"),
                        right.split(".")[-1].strip("[]"),
                    ]
                )
            )
            for left, right in re.findall(
                r"(?i)\b([\w\[\]\.]+)\s*=\s*([\w\[\]\.]+)\b", normalized_sql
            )
            if "." in left and "." in right
        }
        expected_pair = tuple(
            sorted(
                [
                    join_match.group(1).strip("[]"),
                    join_match.group(2).strip("[]"),
                ]
            )
        )
        if expected_pair in actual_join_pairs:
            return True
    return normalized_expected in normalized_sql


def _normalize_result_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        normalized = {
            str(key): "" if value is None else str(value)
            for key, value in dict(row).items()
        }
        normalized_rows.append(normalized)
    normalized_rows.sort(
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
    )
    return normalized_rows


def _result_payload_to_rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [dict(item) for item in result if isinstance(item, dict)]
    return []


def _semantic_ir_projection(question: str) -> dict[str, Any]:
    ir = parse_question_semantics(question)
    blood_values = ir.entity_filters.get("blood_type", ())
    blood_type = {"0": "whole", "1": "component"}.get(
        blood_values[0] if len(blood_values) == 1 else ""
    )
    payload: dict[str, Any] = {
        "metric": ir.metrics[0].name if len(ir.metrics) == 1 else None,
        "dimensions": list(ir.dimensions),
        "year": int(ir.date_start[:4]) if ir.date_start else None,
        "blood_type": blood_type,
    }
    cities = ir.entity_filters.get("city", ())
    if cities:
        payload["filters"] = {"city": cities[0]}
    return payload


async def _execute_eval_sql(sql_runner, ctx, sql: str) -> dict[str, Any]:
    df = await _run_query(sql_runner, sql, ctx)
    rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else []
    result_rows = _result_payload_to_rows(rows)
    result_columns = list(result_rows[0].keys()) if result_rows else []
    return {
        "success": True,
        "rows": result_rows,
        "columns": result_columns,
        "row_count": len(result_rows),
        "error": None,
    }


def _evaluate_sql_case(
    case: dict[str, Any],
    response: dict[str, Any],
    baseline_result: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    question = str(case.get("question") or "").strip()
    should_refuse = bool(case.get("should_refuse"))
    actual_sql = str(response.get("sql") or "")
    actual_columns = [str(item) for item in response.get("result_columns", []) or []]
    actual_row_count = int(response.get("result_row_count", 0) or 0)
    actual_rows = _normalize_result_rows(_result_payload_to_rows(response.get("result")))
    checks: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, expected: Any, actual: Any) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "expected": expected,
                "actual": actual,
            }
        )

    if should_refuse:
        add_check(
            "should_refuse",
            not response.get("success"),
            True,
            bool(response.get("success")),
        )
    else:
        add_check(
            "should_succeed",
            bool(response.get("success")),
            True,
            bool(response.get("success")),
        )
        if bool(case.get("must_execute", True)):
            add_check(
                "execution_success",
                bool(response.get("success")),
                True,
                bool(response.get("success")),
            )
        expected_ir = case.get("expected_semantic_ir")
        if isinstance(expected_ir, dict):
            actual_ir = _semantic_ir_projection(question)
            for key, expected_value in expected_ir.items():
                add_check(
                    f"semantic_ir:{key}",
                    actual_ir.get(key) == expected_value,
                    expected_value,
                    actual_ir.get(key),
                )

    if actual_sql:
        for table in [str(item) for item in case.get("must_include_tables", [])]:
            add_check(
                f"table:{table}",
                _normalized_contains(actual_sql, table),
                table,
                actual_sql,
            )
        for column in [str(item) for item in case.get("must_include_columns", [])]:
            add_check(
                f"column:{column}",
                _normalized_contains(actual_sql, column),
                column,
                actual_sql,
            )
        for filter_text in [str(item) for item in case.get("must_include_filters", [])]:
            add_check(
                f"filter:{filter_text}",
                _normalized_contains(actual_sql, filter_text),
                filter_text,
                actual_sql,
            )
        for join_text in [str(item) for item in case.get("must_include_joins", [])]:
            add_check(
                f"join:{join_text}",
                _normalized_contains(actual_sql, join_text),
                join_text,
                actual_sql,
            )
        for forbidden in [str(item) for item in case.get("must_not_contain", [])]:
            add_check(
                f"not_contains:{forbidden}",
                not _normalized_contains(actual_sql, forbidden),
                forbidden,
                actual_sql,
            )
        for forbidden_column in [
            str(item) for item in case.get("must_not_include_columns", [])
        ]:
            add_check(
                f"not_column:{forbidden_column}",
                not _normalized_contains(actual_sql, forbidden_column),
                forbidden_column,
                actual_sql,
            )
        if "must_have_group_by" in case:
            has_group_by = "group by" in _normalize_assertion_text(actual_sql)
            add_check(
                "group_by",
                has_group_by is bool(case.get("must_have_group_by")),
                bool(case.get("must_have_group_by")),
                has_group_by,
            )
        for expected_column in [
            str(item) for item in case.get("expected_result_columns", []) or []
        ]:
            add_check(
                f"result_column:{expected_column}",
                expected_column in actual_columns,
                expected_column,
                actual_columns,
            )
        if "min_result_rows" in case:
            min_rows = int(case.get("min_result_rows") or 0)
            add_check(
                "min_result_rows",
                actual_row_count >= min_rows,
                min_rows,
                actual_row_count,
            )
        if "max_result_rows" in case:
            max_rows = int(case.get("max_result_rows") or 0)
            add_check(
                "max_result_rows",
                actual_row_count <= max_rows,
                max_rows,
                actual_row_count,
            )
        if baseline_result is not None:
            add_check(
                "baseline_execution_success",
                bool(baseline_result.get("success")),
                True,
                bool(baseline_result.get("success")),
            )
            baseline_columns = [
                str(item) for item in baseline_result.get("columns", []) or []
            ]
            baseline_row_count = int(baseline_result.get("row_count", 0) or 0)
            baseline_rows = _normalize_result_rows(
                _result_payload_to_rows(baseline_result.get("rows"))
            )
            add_check(
                "baseline_result_columns",
                actual_columns == baseline_columns,
                baseline_columns,
                actual_columns,
            )
            add_check(
                "baseline_result_row_count",
                actual_row_count == baseline_row_count,
                baseline_row_count,
                actual_row_count,
            )
            add_check(
                "baseline_result_match",
                actual_rows == baseline_rows,
                baseline_rows,
                actual_rows,
            )
    elif not should_refuse:
        add_check("sql_generated", False, "non-empty sql", actual_sql)

    return {
        "id": str(case.get("id") or ""),
        "split": str(case.get("split") or ""),
        "category": str(case.get("category") or ""),
        "difficulty": str(case.get("difficulty") or ""),
        "question": question,
        "passed": all(item["passed"] for item in checks) if checks else False,
        "should_refuse": should_refuse,
        "actual_sql": actual_sql,
        "executed": bool(not should_refuse and case.get("must_execute", True)),
        "result_columns": actual_columns,
        "result_row_count": actual_row_count,
        "baseline_compared": baseline_result is not None,
        "baseline_error": None if baseline_result is None else baseline_result.get("error"),
        "error": response.get("error"),
        "checks": checks,
    }


async def run_evaluation_suite(split: str | None = None) -> list[dict[str, Any]]:
    split = split or settings.training_eval_split
    if split not in {"dev", "test"}:
        raise ValueError("evaluation split must be 'dev' or 'test'")
    path = Path(
        settings.eval_test_set_path if split == "test" else settings.eval_dev_set_path
    )
    try:
        loaded_cases = load_evaluation_cases(path, expected_split=split)
    except Exception as exc:
        logger.warning("评测集读取失败: %s", exc)
        return []
    cases = [item.payload for item in loaded_cases]

    from ..application.text2sql_service import generate_sql_with_feedback

    results: list[dict[str, Any]] = []
    sql_runner = get_sql_runner()
    eval_ctx = ToolContext(
        user=User(id="evaluator", username="evaluator"),
        conversation_id="evaluation",
        request_id="evaluation",
        agent_memory=get_agent_memory(),
    )
    for index, case in enumerate(cases, start=1):
        question = str(case.get("question") or "").strip()
        if not question:
            continue
        should_refuse = bool(case.get("should_refuse"))
        response = await generate_sql_with_feedback(
            question=question,
            max_retries=1,
            execute_sql=not should_refuse and bool(case.get("must_execute", True)),
            capture_feedback=False,
        )
        baseline_result: Optional[dict[str, Any]] = None
        baseline_sql = str(case.get("baseline_sql") or "").strip()
        if (
            baseline_sql
            and not should_refuse
            and bool(case.get("must_execute", True))
            and bool(response.get("success"))
        ):
            try:
                baseline_result = await _execute_eval_sql(sql_runner, eval_ctx, baseline_sql)
            except Exception as exc:
                baseline_result = {
                    "success": False,
                    "rows": [],
                    "columns": [],
                    "row_count": 0,
                    "error": str(exc),
                }
        case_result = _evaluate_sql_case(case, response, baseline_result)
        case_result["case_index"] = index
        results.append(case_result)
    return results


def train_knowledge(
    include_samples: bool = True,
    sample_rows: int = 10,
) -> None:
    """增强版训练入口"""
    asyncio.run(_train_async(include_samples, sample_rows))


async def _train_async(
    include_samples: bool,
    sample_rows: int,
) -> None:
    logger.info("=== 开始增强版知识训练 ===")
    live_schema = await get_live_schema(force_refresh=True)
    fingerprint = _build_training_fingerprint(
        include_samples=include_samples,
        sample_rows=sample_rows,
        live_schema_hash=_hash_live_schema(live_schema),
    )
    if _should_skip_training(fingerprint):
        logger.info("训练输入未变化，跳过本次重训。")
        return

    index_records: list[dict[str, Any]] = []
    report = _empty_training_report(include_samples, sample_rows)

    sql_runner = get_sql_runner()

    knowledge_memory = get_knowledge_memory()

    agent_memory = get_agent_memory()

    ctx = ToolContext(
        user=User(id="trainer", username="admin"),
        conversation_id="training",
        request_id="train",
        agent_memory=agent_memory,
    )

    # 在训练前清空知识库与 agent 记忆，避免旧数据干扰生成。
    logger.info("正在清空旧的向量知识库与 agent 记忆...")
    await knowledge_memory.clear_memories(ctx)
    await agent_memory.clear_memories(ctx)

    _build_table_schema_index_records(live_schema, index_records, report)

    # 1. 训练表角色与主粒度
    await _train_table_roles(live_schema, knowledge_memory, ctx, index_records)

    # 2. 训练样本数据画像
    if include_samples:
        await _train_sample_data(
            sql_runner,
            knowledge_memory,
            ctx,
            sample_rows,
            index_records,
            report,
            live_schema,
        )

    # 3. 训练 join 路径
    await _train_join_paths(live_schema, knowledge_memory, ctx, index_records)

    # 4. 加载业务文档与 DDL 文档
    bundle = load_knowledge_bundle(settings.structured_knowledge_dir, live_schema)
    if not bundle.available:
        raise FileNotFoundError(
            f"结构化知识库不存在或为空: {settings.structured_knowledge_dir}"
        )
    await _train_structured_knowledge(
        knowledge_memory, ctx, bundle, index_records, report
    )

    # 5. 加载运行期反馈样本
    await _train_feedback_examples(knowledge_memory, ctx, index_records, report)

    index_records, removed_count = _dedupe_index_records(index_records)
    report["deduped_records_removed"] = removed_count
    _flush_knowledge_index(index_records)
    reset_schema_cache()
    report = _finalize_training_report(report, index_records)
    evaluations = await run_evaluation_suite()
    report["evaluations"] = evaluations
    report["evaluation_split"] = settings.training_eval_split
    report["evaluation_summary"] = {
        "total": len(evaluations),
        "passed": sum(1 for item in evaluations if item.get("passed")),
        "failed": sum(1 for item in evaluations if not item.get("passed")),
    }
    _write_training_manifest(
        include_samples=include_samples,
        sample_rows=sample_rows,
        index_records=index_records,
        report=report,
        fingerprint=fingerprint,
    )
    _write_json_file(settings.training_report_path, report)
    _write_training_state(fingerprint, report)

    logger.info("=== 增强版知识训练完成 ===")


async def _train_table_roles(live_schema, knowledge_memory, ctx, index_records):
    logger.info("正在训练表角色与主粒度...")
    for table_name, info in sorted(live_schema.items()):
        if not _should_include_table(table_name):
            continue
        roles, granularity = _infer_table_role(table_name, info)
        dimensions = [
            name
            for name, col in info["columns"].items()
            if "dimension"
            in _column_role_tags(
                name, col.get("description", ""), col.get("data_type", "")
            )
        ]
        measures = [
            name
            for name, col in info["columns"].items()
            if "measure"
            in _column_role_tags(
                name, col.get("description", ""), col.get("data_type", "")
            )
        ]
        text = (
            "表角色与分析粒度:\n"
            f"表: {table_name}\n"
            f"角色: {'、'.join(roles)}\n"
            f"主粒度: {granularity}\n"
            f"常见维度字段: {'、'.join(dimensions[:8]) if dimensions else '未识别'}\n"
            f"常见指标字段: {'、'.join(measures[:8]) if measures else '未识别'}"
        )
        await _save_training_text(
            knowledge_memory,
            text,
            ctx,
            index_records,
            source_type="table_role",
            table_names=[table_name],
            field_names=dimensions[:8] + measures[:8],
            role_tags=roles,
            dimension_tags=dimensions[:8],
            metric_tags=measures[:8],
            granularity=granularity,
            confidence=88,
        )


async def _profile_distinct_values(
    sql_runner, ctx, table: str, column: str, limit: int
) -> list[str]:
    sql = (
        f"SELECT TOP {limit} CAST([{column}] AS NVARCHAR(255)) AS value "
        f"FROM [{table}] WHERE [{column}] IS NOT NULL "
        f"GROUP BY [{column}] ORDER BY COUNT(1) DESC"
    )
    df = await _run_query(sql_runner, sql, ctx)
    if df.empty:
        return []
    return [str(value).strip() for value in df["value"].tolist() if str(value).strip()]


async def _profile_min_max(sql_runner, ctx, table: str, column: str) -> tuple[str, str]:
    sql = (
        f"SELECT MIN([{column}]) AS min_value, MAX([{column}]) AS max_value "
        f"FROM [{table}] WHERE [{column}] IS NOT NULL"
    )
    df = await _run_query(sql_runner, sql, ctx)
    if df.empty:
        return "", ""
    row = df.iloc[0]
    return str(row.get("min_value") or ""), str(row.get("max_value") or "")


def _is_categorical_column(column_name: str, data_type: str) -> bool:
    name = column_name.lower()
    dtype = data_type.lower()
    if any(
        token in name
        for token in [
            "type",
            "status",
            "flag",
            "code",
            "city",
            "district",
            "sex",
            "blood",
        ]
    ):
        return True
    return dtype in {"char", "nchar", "varchar", "nvarchar"}


def _is_time_column(column_name: str, data_type: str) -> bool:
    name = column_name.lower()
    dtype = data_type.lower()
    return (
        "date" in name
        or "time" in name
        or dtype in {"date", "datetime", "datetime2", "smalldatetime"}
    )


def _is_numeric_column(data_type: str) -> bool:
    return data_type.lower() in {
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
    }


def _profile_modes_for_column(
    column_name: str, data_type: str, description: str
) -> tuple[list[str], int]:
    modes: list[str] = []
    score = 0
    role_tags = _column_role_tags(column_name, description, data_type)
    if _is_time_column(column_name, data_type):
        modes.append("time_range")
        score += 100
    if _is_categorical_column(column_name, data_type):
        modes.append("categorical")
        score += 80
    if _is_numeric_column(data_type):
        modes.append("numeric_range")
        score += 60
    if description:
        score += 10
    if "dimension" in role_tags:
        score += 12
    if "enum_candidate" in role_tags:
        score += 8
    if "measure" in role_tags:
        score += 12
    return _dedupe_keep_order(modes), score


def _select_profile_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        column_name = str(row.get("COLUMN_NAME") or "")
        data_type = str(row.get("DATA_TYPE") or "")
        description = str(row.get("COLUMN_DESCRIPTION") or "")
        modes, score = _profile_modes_for_column(column_name, data_type, description)
        if not column_name or not modes:
            continue
        candidates.append(
            {
                "column_name": column_name,
                "data_type": data_type,
                "description": description,
                "modes": modes,
                "score": score,
            }
        )
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["column_name"])))
    limit = max(1, settings.profiling_max_columns_per_table)
    return candidates[:limit]


async def _train_sample_data(
    sql_runner,
    knowledge_memory,
    ctx,
    sample_rows: int,
    index_records,
    report,
    live_schema,
):
    logger.info("正在训练字段数据画像...")
    profile_records = 0
    table_columns: dict[str, list[dict[str, Any]]] = {}
    for table_name, info in live_schema.items():
        if not _should_include_table(table_name):
            continue
        table_columns[table_name] = [
            {
                "COLUMN_NAME": column_name,
                "DATA_TYPE": str(column_info.get("data_type") or ""),
                "COLUMN_DESCRIPTION": str(column_info.get("description") or ""),
            }
            for column_name, column_info in info.get("columns", {}).items()
        ]

    if not table_columns:
        return

    selected_columns_by_table: dict[str, list[dict[str, Any]]] = {}
    table_order: list[str] = []
    if settings.sample_tables:
        table_order = [
            item.strip()
            for item in settings.sample_tables.split(",")
            if item.strip() and item.strip() in table_columns
        ]
    else:
        table_order = sorted(table_columns.keys())

    for table in table_order:
        selected_columns = _select_profile_columns(table_columns.get(table, []))
        if selected_columns:
            selected_columns_by_table[table] = selected_columns

    ordered_tables = sorted(
        selected_columns_by_table.keys(),
        key=lambda table: (-len(selected_columns_by_table[table]), table),
    )
    if settings.sample_tables:
        ordered_tables = [
            table for table in table_order if table in selected_columns_by_table
        ]
    if settings.profiling_max_tables > 0:
        ordered_tables = ordered_tables[: settings.profiling_max_tables]

    report["profiling_tables_considered"] = len(ordered_tables)
    report["profiling_columns_selected"] = sum(
        len(selected_columns_by_table[table]) for table in ordered_tables
    )

    for table in ordered_tables:
        if not _should_include_table(table):
            continue
        try:
            for column_info in selected_columns_by_table.get(table, []):
                column_name = str(column_info["column_name"])
                data_type = str(column_info["data_type"])
                modes = list(column_info["modes"])

                if "categorical" in modes:
                    values = await _profile_distinct_values(
                        sql_runner,
                        ctx,
                        table,
                        column_name,
                        min(sample_rows, settings.profiling_max_distinct_values),
                    )
                    if values:
                        text = (
                            f"字段画像:\n表: {table}\n字段: {column_name}\n"
                            f"类型: {data_type}\n高频离散值: {'、'.join(values)}"
                        )
                        await _save_training_text(
                            knowledge_memory,
                            text,
                            ctx,
                            index_records,
                            source_type="sample_values",
                            table_names=[table],
                            field_names=[column_name],
                            aliases=values,
                            enum_values=values,
                            profile_tags=["categorical"],
                            confidence=74,
                        )
                        profile_records += 1

                if "time_range" in modes:
                    min_value, max_value = await _profile_min_max(
                        sql_runner, ctx, table, column_name
                    )
                    if min_value or max_value:
                        text = (
                            f"字段画像:\n表: {table}\n字段: {column_name}\n"
                            f"类型: {data_type}\n时间范围: {min_value} ~ {max_value}"
                        )
                        await _save_training_text(
                            knowledge_memory,
                            text,
                            ctx,
                            index_records,
                            source_type="column_profile",
                            table_names=[table],
                            field_names=[column_name],
                            time_tags=[column_name],
                            profile_tags=["time_range"],
                            confidence=72,
                        )
                        profile_records += 1

                if "numeric_range" in modes:
                    min_value, max_value = await _profile_min_max(
                        sql_runner, ctx, table, column_name
                    )
                    if min_value or max_value:
                        text = (
                            f"字段画像:\n表: {table}\n字段: {column_name}\n"
                            f"类型: {data_type}\n数值范围: {min_value} ~ {max_value}"
                        )
                        await _save_training_text(
                            knowledge_memory,
                            text,
                            ctx,
                            index_records,
                            source_type="column_profile",
                            table_names=[table],
                            field_names=[column_name],
                            metric_tags=[column_name],
                            profile_tags=["numeric_range"],
                            confidence=70,
                        )
                        profile_records += 1
        except Exception as exc:
            logger.warning("训练字段画像失败 %s: %s", table, exc)
            report["warnings"].append(f"字段画像失败 {table}: {exc}")
    report["profile_records"] = profile_records


async def _train_join_paths(live_schema, knowledge_memory, ctx, index_records):
    logger.info("正在训练 join 路径...")
    for table_name, info in sorted(live_schema.items()):
        if not _should_include_table(table_name):
            continue
        for fk in info.get("foreign_keys", []):
            referenced_table = str(fk.get("referenced_table") or "")
            if not referenced_table or not _should_include_table(referenced_table):
                continue
            column_name = str(fk.get("column_name") or "")
            referenced_columns = set(
                live_schema.get(referenced_table, {}).get("columns", {}).keys()
            )
            referenced_column = (
                column_name if column_name in referenced_columns else "主键"
            )
            join_text = (
                "推荐关联路径:\n"
                f"事实表: {table_name}\n"
                f"关联字段: {column_name}\n"
                f"维度表: {referenced_table}\n"
                f"推荐写法: {table_name}.{column_name} = {referenced_table}.{referenced_column}"
            )
            await _save_training_text(
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


async def _train_feedback_examples(knowledge_memory, ctx, index_records, report):
    logger.info("正在加载运行期反馈正确样本...")
    raw_count = 0
    feedback_path = Path(settings.feedback_examples_path)
    if feedback_path.exists():
        for line in feedback_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("question") and payload.get("sql"):
                raw_count += 1
    feedback_examples = load_gold_examples()
    report["feedback_examples_loaded"] = len(feedback_examples)
    report["feedback_examples_rejected"] = max(raw_count - len(feedback_examples), 0)
    if not feedback_examples:
        return
    for item in feedback_examples:
        question = str(item.get("question") or "").strip()
        sql = str(item.get("sql") or "").strip()
        if not question or not sql:
            continue
        metadata = _question_sql_metadata(question, sql)
        await _save_training_text(
            knowledge_memory,
            f"运行期反馈正确样本:\n问题: {question}\nSQL:\n{sql}",
            ctx,
            index_records,
            source_type="feedback_example",
            table_names=metadata["sql_tables"],
            aliases=_question_aliases(question),
            metric_tags=_metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=_time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            join_tables=metadata["sql_tables"],
            confidence=98,
        )
        report["feedback_examples"] += 1


async def _train_structured_knowledge(
    knowledge_memory,
    ctx,
    bundle: KnowledgeBundle,
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
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
        await _save_training_text(
            knowledge_memory, text, ctx, index_records,
            source_type="table_card", table_names=[table],
            field_names=card.get("important_columns", []),
            aliases=card.get("business_aliases", []),
            metric_tags=card.get("metrics", []),
            dimension_tags=card.get("dimensions", []), confidence=95,
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
        await _save_training_text(
            knowledge_memory, text, ctx, index_records,
            source_type="metric_rule", table_names=[table], field_names=[column],
            aliases=metric.get("aliases", []),
            metric_tags=[str(metric.get("id") or metric.get("name"))], confidence=98,
        )
        trained += 1

    for dimension in bundle.dimensions:
        table = str(dimension.get("table") or "")
        text = (
            f"维度定义: {dimension.get('name')}\n"
            f"别名: {'、'.join(dimension.get('aliases', []))}\n"
            f"来源: {table}.{','.join(dimension.get('columns', []))}"
        )
        await _save_training_text(
            knowledge_memory, text, ctx, index_records,
            source_type="dimension_rule", table_names=[table],
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
        await _save_training_text(
            knowledge_memory, text, ctx, index_records,
            source_type="join_path", table_names=tables, join_tables=tables,
            field_names=[str(join["left_column"]), str(join["right_column"])],
            confidence=99,
        )
        trained += 1

    for policy in bundle.policies:
        text = "业务策略: " + json.dumps(policy, ensure_ascii=False, sort_keys=True)
        await _save_training_text(
            knowledge_memory, text, ctx, index_records,
            source_type="domain_policy",
            table_names=[str(policy["table"])] if policy.get("table") else None,
            field_names=[str(policy["column"])] if policy.get("column") else None,
            aliases=policy.get("terms", []), confidence=98,
        )
        trained += 1

    pairs = [(str(item["question"]), str(item["sql"])) for item in bundle.gold_sql]
    await _train_question_sql_examples(
        knowledge_memory, ctx, pairs, index_records, report
    )
    trained += len(pairs)
    for refusal in bundle.refusals:
        await _save_training_text(
            knowledge_memory,
            f"拒答示例\n问题: {refusal.get('question')}\n原因: {refusal.get('reason')}",
            ctx, index_records, source_type="refusal_example", confidence=98,
        )
        trained += 1
    report["question_sql_examples"] = len(pairs)
    report["structured_records"] = trained




async def _run_query(sql_runner, sql: str, ctx):
    args = RunSqlToolArgs(sql=sql)
    return await sql_runner.run_sql(args, ctx)
