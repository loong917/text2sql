# src/services/training.py
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

from ..core.agent import (
    get_sql_runner,
    get_knowledge_memory,
    get_agent_memory,
)
from ..core.config import settings
from ..core.logging import setup_logging
from .schema_service import get_live_schema, reset_schema_cache

logger = setup_logging("text2sql.training")
TRAINING_FINGERPRINT_VERSION = 4
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
    "ddl_chunk": 40,
    "business_doc_chunk": 35,
    "business_rule": 30,
}

"""领域同义词规则"""
DOMAIN_SYNONYM_RULES = [
    {
        "term": "机构",
        "aliases": ["单位", "血站", "采血机构", "采血点", "机构名称", "机构编号"],
        "hint": "优先联想到机构维度，通常对应 Pub_OrgAddress、BTSID、BTSName、OrgName、InstID。",
    },
    {
        "term": "城市",
        "aliases": ["地区", "地市", "城市名称", "所属城市"],
        "hint": "优先对应机构主数据中的 City 字段。",
    },
    {
        "term": "采集人次",
        "aliases": ["采血人次", "献血人次", "人次", "次数"],
        "hint": "通常表示计数类指标，SQL 上优先考虑 COUNT(*)。",
    },
    {
        "term": "采集量",
        "aliases": ["采血量", "献血量", "血量", "总量"],
        "hint": "通常表示汇总类指标，SQL 上优先考虑 SUM(BCPVolume)。",
    },
    {
        "term": "采集日期",
        "aliases": ["采血日期", "献血日期", "采集时间", "采血时间"],
        "hint": "通常对应 Stat_Collection.BCDate。",
    },
    {
        "term": "全血",
        "aliases": ["全血采集", "全血献血", "全血业务"],
        "hint": "通常对应 BCType = 0。",
    },
    {
        "term": "成分血",
        "aliases": ["机采", "成分献血", "成分血采集", "成分血业务"],
        "hint": "通常对应 BCType = 1。",
    },
]

"""指标规则"""
DOMAIN_METRIC_RULES = [
    {
        "metric": "采集人次",
        "aliases": ["采血人次", "献血人次", "次数"],
        "sql_hint": "使用 COUNT(*) 或按主键计数。",
    },
    {
        "metric": "采集量",
        "aliases": ["采血量", "献血量", "血量"],
        "sql_hint": "使用 SUM(BCPVolume)。",
    },
]

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


def _clean_heading_text(text: str) -> str:
    cleaned = re.sub(r"^\d+(?:\.\d+)*\s*", "", text.strip())
    return cleaned.strip(" :：")


def _extract_business_table_defs(content: str) -> list[dict]:
    tables: list[dict] = []
    current: Optional[dict] = None
    current_section = ""

    for raw_line in content.splitlines():
        stripped = raw_line.strip()

        if stripped.startswith("# ") and "查询示例" in stripped:
            if current:
                tables.append(current)
            break

        if stripped.startswith("# "):
            if current:
                tables.append(current)
            title = _clean_heading_text(stripped.removeprefix("# "))
            title = re.sub(r"^\d+[、.]\s*", "", title)
            current = {
                "title": title,
                "table_name": title.split()[-1].strip(),
                "business_meaning": "",
                "fields": [],
            }
            current_section = ""
            continue

        if stripped.startswith("### ") and "问题" not in stripped:
            heading = _clean_heading_text(stripped.removeprefix("### "))
            if re.search(r"业务含义$", heading):
                current_section = "business_meaning"
                continue
            if re.search(r"字段含义$", heading):
                current_section = "fields"
                continue

            if current:
                tables.append(current)
            current = {
                "title": heading,
                "table_name": "",
                "business_meaning": "",
                "fields": [],
            }
            current_section = ""
            continue

        if not current:
            continue

        if "表名:" in stripped:
            current["table_name"] = stripped.split("表名:", 1)[1].strip()
            continue

        if "业务含义:" in stripped:
            current["business_meaning"] = stripped.split("业务含义:", 1)[1].strip()
            continue

        if (
            current_section == "business_meaning"
            and stripped
            and not stripped.startswith("```")
        ):
            current["business_meaning"] = stripped
            continue

        if stripped.startswith("- "):
            field_text = stripped[2:].strip()
            if ":" in field_text:
                field_name, description = field_text.split(":", 1)
            elif " " in field_text:
                field_name, description = field_text.split(None, 1)
            else:
                continue
            current["fields"].append(
                {
                    "field_name": field_name.strip(),
                    "description": description.strip(),
                }
            )

    if current:
        tables.append(current)

    return tables


def _extract_ddl_table_defs(content: str) -> dict[str, dict]:
    table_defs: dict[str, dict] = {}
    section_matches = list(re.finditer(r"(?m)^#\s+(.+)$", content))
    for idx, match in enumerate(section_matches):
        heading = _clean_heading_text(match.group(1))
        heading = re.sub(r"^\d+[、.]\s*", "", heading)
        table_name = heading.split()[-1].strip()
        start = match.end()
        end = (
            section_matches[idx + 1].start()
            if idx + 1 < len(section_matches)
            else len(content)
        )
        body = content[start:end]

        info = {
            "table_name": table_name,
            "business_meaning": "",
            "table_description": "",
            "columns": {},
        }

        meaning_match = re.search(
            r"###\s+[^\n]*业务含义\s*(?P<meaning>.*?)(?=\n###|\Z)",
            body,
            re.DOTALL,
        )
        if meaning_match:
            meaning_lines = [
                line.strip()
                for line in meaning_match.group("meaning").splitlines()
                if line.strip() and not line.strip().startswith("```")
            ]
            if meaning_lines:
                info["business_meaning"] = meaning_lines[0]
                info["table_description"] = meaning_lines[0]

        create_match = re.search(
            r"CREATE\s+TABLE\s+[^\(]+\((?P<columns>.*?)\)\s*;",
            body,
            re.DOTALL | re.IGNORECASE,
        )
        if create_match:
            for line in create_match.group("columns").splitlines():
                stripped = line.strip().rstrip(",")
                if (
                    not stripped
                    or stripped.upper().startswith("CONSTRAINT")
                    or stripped.upper().startswith("PRIMARY KEY")
                ):
                    continue
                column_name = stripped.split()[0]
                info["columns"].setdefault(column_name, {"description": ""})

        in_field_section = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("### "):
                heading_text = _clean_heading_text(stripped.removeprefix("### "))
                in_field_section = bool(re.search(r"字段含义$", heading_text))
                continue
            if not in_field_section or not stripped.startswith("- "):
                continue
            field_text = stripped[2:].strip()
            if " " not in field_text:
                continue
            field_name, description = field_text.split(None, 1)
            info["columns"].setdefault(field_name, {"description": ""})
            info["columns"][field_name]["description"] = description.strip()

        table_defs[table_name] = info

    return table_defs


def _table_aliases(
    title: str, business_meaning: str, table_description: str
) -> list[str]:
    aliases = [title, table_description]
    if title.endswith("表"):
        aliases.append(title[:-1])
    if title.endswith("字典表"):
        aliases.append(title.replace("字典表", "字典"))
    core_meaning = _first_sentence(business_meaning)
    aliases.append(core_meaning)
    if core_meaning.startswith("记录") and core_meaning.endswith("的基本信息"):
        aliases.append(core_meaning[2:-5] + "信息")
    return _dedupe_keep_order(aliases)


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


def _enum_value_aliases(label: str) -> list[str]:
    aliases = [label]
    mapping = {
        "全血": ["全血采集", "全血献血"],
        "成分血": ["机采", "成分献血", "成分血采集"],
        "阴性": ["Rh阴性", "阴性血型"],
        "阳性": ["Rh阳性", "阳性血型"],
        "男": ["男性"],
        "女": ["女性"],
        "有效": ["启用", "正常有效"],
        "无效": ["停用", "失效"],
        "是": ["true", "已启用"],
        "否": ["false", "未启用"],
    }
    aliases.extend(mapping.get(label, []))
    return _dedupe_keep_order(aliases)


def _extract_enum_mappings(description: str) -> list[tuple[str, str]]:
    normalized = (
        description.replace("；", "：")
        .replace(";", "：")
        .replace("。", " ")
        .replace("，", " ")
        .replace(",", " ")
    )
    pairs = re.findall(r"([0-9A-Za-z]+)\s*[：:]\s*([^\s]+)", normalized)
    return [(code.strip(), label.strip()) for code, label in pairs if code and label]


def _render_alias_memories(
    business_tables: list[dict], ddl_tables: dict[str, dict]
) -> list[str]:
    memories: list[str] = []

    for business_table in business_tables:
        table_name = business_table.get("table_name", "")
        ddl_table = ddl_tables.get(table_name, {})
        table_aliases = _table_aliases(
            business_table.get("title", ""),
            business_table.get("business_meaning", "")
            or str(ddl_table.get("business_meaning", "")),
            str(ddl_table.get("table_description", "")),
        )

        if table_aliases:
            memories.append(
                "中文别名词典:\n"
                f"表英文名: {table_name}\n"
                f"表中文别名: {'、'.join(table_aliases)}"
            )

        for field in business_table.get("fields", []):
            field_name = field.get("field_name", "")
            business_desc = field.get("description", "")
            ddl_desc = (
                ddl_table.get("columns", {}).get(field_name, {}).get("description", "")
                if ddl_table
                else ""
            )
            merged_desc = business_desc or ddl_desc
            aliases = _field_aliases(merged_desc)
            if not field_name or not aliases:
                continue

            memories.append(
                "中文字段别名词典:\n"
                f"表: {table_name}\n"
                f"字段: {field_name}\n"
                f"字段说明: {merged_desc}\n"
                f"中文别名: {'、'.join(aliases)}"
            )

    return memories


def _question_templates(question: str) -> list[str]:
    base = re.sub(r"\b(19|20)\d{2}\b", "{年份}", question).strip()
    city_terms: list[str] = []
    for rule in DOMAIN_SYNONYM_RULES:
        term = str(rule.get("term") or "").strip()
        aliases = [str(item).strip() for item in rule.get("aliases", [])]
        if term.endswith("市"):
            city_terms.extend([term, *aliases])
    for city_term in _dedupe_keep_order(city_terms):
        if city_term and city_term in base:
            base = base.replace(city_term, "{城市}")
    base = re.sub(r"[\u4e00-\u9fff]{1,12}(区|县|市)", "{地区}", base)
    variants = [base]
    replacement_groups = [
        ("统计", ["查询", "汇总"]),
        ("每个", ["各"]),
        ("机构", ["单位", "血站"]),
        ("城市", ["地区", "地市"]),
        ("全血", ["全血采集"]),
        ("成分血", ["机采", "成分献血"]),
        ("采集人次", ["采血人次", "献血人次"]),
        ("采集量", ["采血量", "献血量"]),
    ]

    for old, replacements in replacement_groups:
        snapshot = list(variants)
        for candidate in snapshot:
            if old not in candidate:
                continue
            for new in replacements:
                variants.append(candidate.replace(old, new))
                if len(variants) >= 10:
                    return _dedupe_keep_order(variants)[:8]

    return _dedupe_keep_order(variants)[:8]


def _generalize_question_pattern(question: str, metadata: dict[str, Any]) -> str:
    pattern = _normalize_training_question(question)
    pattern = re.sub(r"\b(19|20)\d{2}\b", "{年份}", pattern)
    pattern = re.sub(r"(19|20)\d{2}年", "{年份}", pattern)
    for rule in DOMAIN_SYNONYM_RULES:
        term = str(rule.get("term") or "").strip()
        aliases = [str(item).strip() for item in rule.get("aliases", [])]
        if term.endswith("市"):
            for city_term in _dedupe_keep_order([term, *aliases]):
                if city_term and city_term in pattern:
                    pattern = pattern.replace(city_term, "{城市}")
    filters = set(metadata.get("filters", []) or [])
    if "全血" in filters:
        pattern = pattern.replace("全血", "{采集类型}")
    if "成分血" in filters:
        pattern = pattern.replace("成分血", "{采集类型}")
    metric = str(metadata.get("metric") or "").strip()
    if metric == "采集人次":
        pattern = pattern.replace("采集人次", "{指标}")
        pattern = pattern.replace("献血人次", "{指标}")
        pattern = pattern.replace("人次", "{指标}")
        pattern = pattern.replace("次数", "{指标}")
    elif metric == "采集量":
        pattern = pattern.replace("采集量", "{指标}")
        pattern = pattern.replace("采血量", "{指标}")
        pattern = pattern.replace("血量", "{指标}")
        pattern = pattern.replace("总量", "{指标}")
    return pattern


def _parameterize_sql_template(sql: str) -> str:
    template = re.sub(r"\s+", " ", sql.strip())
    dates = re.findall(r"'(\d{4}-\d{2}-\d{2})'", template)
    if len(dates) >= 2:
        template = template.replace(f"'{dates[0]}'", "'{开始日期}'", 1)
        template = template.replace(f"'{dates[1]}'", "'{结束日期}'", 1)
    template = re.sub(r"=\s*'([^']*市)'", "= '{城市}'", template)
    template = re.sub(
        r"BCType\s*=\s*'0'", "BCType = '{采集类型编码}'", template, flags=re.I
    )
    template = re.sub(
        r"BCType\s*=\s*'1'", "BCType = '{采集类型编码}'", template, flags=re.I
    )
    return template


def _extract_years_from_question_pairs(
    question_sql_pairs: list[tuple[str, str]],
) -> list[int]:
    years: list[int] = []
    for question, sql in question_sql_pairs:
        for year_text in re.findall(r"\b((?:19|20)\d{2})\b", question):
            years.append(int(year_text))
        for year_text in re.findall(r"'((?:19|20)\d{2})-\d{2}-\d{2}'", sql):
            years.append(int(year_text))
    return sorted(set(years))


def _expansion_city_terms() -> list[str]:
    cities: list[str] = []
    for rule in DOMAIN_SYNONYM_RULES:
        term = str(rule.get("term") or "").strip()
        if term.endswith("市"):
            cities.append(term)
    return _dedupe_keep_order(cities)


def _build_year_range(year: int) -> tuple[str, str]:
    return f"{year}-01-01", f"{year + 1}-01-01"


def _preferred_table_name(
    live_schema: dict[str, dict[str, Any]], candidates: list[str]
) -> Optional[str]:
    for candidate in candidates:
        if candidate in live_schema:
            return candidate
    return None


def _find_table_by_columns(
    live_schema: dict[str, dict[str, Any]], required_columns: list[str]
) -> Optional[str]:
    required = {item.lower() for item in required_columns}
    for table_name, info in live_schema.items():
        columns = {column.lower() for column in info.get("columns", {})}
        if required.issubset(columns):
            return table_name
    return None


def _resolve_schema_expansion_context(
    live_schema: dict[str, dict[str, Any]], ddl_tables: dict[str, dict[str, Any]]
) -> Optional[dict[str, Any]]:
    fact_table = _preferred_table_name(
        live_schema, ["Stat_Collection"]
    ) or _find_table_by_columns(live_schema, ["BCDate", "BCType"])
    dimension_table = _preferred_table_name(
        live_schema, ["Pub_OrgAddress"]
    ) or _find_table_by_columns(live_schema, ["InstID", "OrgName"])
    if not fact_table or not dimension_table:
        return None

    fact_columns = live_schema.get(fact_table, {}).get("columns", {})
    dimension_columns = live_schema.get(dimension_table, {}).get("columns", {})
    if not fact_columns or not dimension_columns:
        return None

    join_left = "BTSID" if "BTSID" in fact_columns else ""
    join_right = "InstID" if "InstID" in dimension_columns else ""
    if not join_left or not join_right:
        for fk in live_schema.get(fact_table, {}).get("foreign_keys", []):
            if str(fk.get("referenced_table") or "") != dimension_table:
                continue
            candidate_left = str(fk.get("column_name") or "")
            if candidate_left and candidate_left in fact_columns:
                join_left = candidate_left
                break
        if not join_right:
            join_right = "主键"
    if not join_left or not join_right:
        return None

    dimensions: list[dict[str, Any]] = []
    if {"InstID", "OrgName"}.issubset(set(dimension_columns)):
        dimensions.append(
            {
                "name": "机构",
                "question_label": "各机构",
                "select_expr": "b.InstID AS InstID, b.OrgName AS InstName",
                "group_by_expr": "b.InstID, b.OrgName",
                "supports_city_filter": "City" in dimension_columns,
            }
        )
    if "City" in dimension_columns:
        dimensions.append(
            {
                "name": "城市",
                "question_label": "各城市",
                "select_expr": "b.City AS City",
                "group_by_expr": "b.City",
                "supports_city_filter": False,
            }
        )
    if "District" in dimension_columns:
        dimensions.append(
            {
                "name": "区县",
                "question_label": "各区县",
                "select_expr": "b.District AS District",
                "group_by_expr": "b.District",
                "supports_city_filter": "City" in dimension_columns,
            }
        )
    if not dimensions:
        return None

    metrics = [
        {
            "name": "采集人次",
            "question_label": "采集人次",
            "aggregate_expr": "COUNT(*) AS Times",
        }
    ]
    if "BCPVolume" in fact_columns:
        metrics.append(
            {
                "name": "采集量",
                "question_label": "采集量",
                "aggregate_expr": "SUM(a.BCPVolume) AS Volume",
            }
        )

    collection_type_field = "BCType" if "BCType" in fact_columns else ""
    collection_types: list[dict[str, str]] = []
    enum_pairs = []
    ddl_table = ddl_tables.get(fact_table, {})
    if ddl_table:
        enum_pairs = _extract_enum_mappings(
            str(
                ddl_table.get("columns", {})
                .get(collection_type_field, {})
                .get("description", "")
            )
        )
    if enum_pairs:
        for code, label in enum_pairs:
            if label in {"全血", "成分血"}:
                collection_types.append(
                    {"name": label, "question_label": label, "code": code}
                )
    if not collection_types and collection_type_field:
        collection_types = [
            {"name": "全血", "question_label": "全血", "code": "0"},
            {"name": "成分血", "question_label": "成分血", "code": "1"},
        ]

    if not collection_types:
        return None

    return {
        "fact_table": fact_table,
        "dimension_table": dimension_table,
        "join_left": join_left,
        "join_right": join_right,
        "date_field": "BCDate" if "BCDate" in fact_columns else "",
        "city_field": "City" if "City" in dimension_columns else "",
        "dimensions": dimensions,
        "metrics": metrics,
        "collection_types": collection_types,
    }


def _build_schema_driven_question(
    *,
    schema_context: dict[str, Any],
    city_filter: Optional[str],
    year: int,
    collection_type: dict[str, str],
    metric: dict[str, str],
    dimension: dict[str, Any],
) -> str:
    if city_filter and dimension.get("supports_city_filter"):
        return (
            f"统计{city_filter}{dimension['question_label']}{year}年的"
            f"{collection_type['question_label']}{metric['question_label']}"
        )
    return (
        f"统计{dimension['question_label']}{year}年的"
        f"{collection_type['question_label']}{metric['question_label']}"
    )


def _build_schema_driven_sql(
    *,
    schema_context: dict[str, Any],
    city_filter: Optional[str],
    year: int,
    collection_type: dict[str, str],
    metric: dict[str, str],
    dimension: dict[str, Any],
) -> str:
    start_date, end_date = _build_year_range(year)
    where_clauses = [
        f"a.{schema_context['date_field']} >= '{start_date}'",
        f"a.{schema_context['date_field']} < '{end_date}'",
        f"a.BCType = '{collection_type['code']}'",
    ]
    city_field = str(schema_context.get("city_field") or "")
    if city_filter and dimension.get("supports_city_filter") and city_field:
        where_clauses.append(f"b.{city_field} = '{city_filter}'")
    return (
        f"SELECT {dimension['select_expr']}, {metric['aggregate_expr']} "
        f"FROM {schema_context['fact_table']} a "
        f"JOIN {schema_context['dimension_table']} b ON a.{schema_context['join_left']} = b.{schema_context['join_right']} "
        f"WHERE {' AND '.join(where_clauses)} "
        f"GROUP BY {dimension['group_by_expr']}"
    )


def _generate_expanded_question_sql_pairs(
    question_sql_pairs: list[tuple[str, str]],
    live_schema: dict[str, dict[str, Any]],
    ddl_tables: dict[str, dict[str, Any]],
    max_expanded_pairs: int = 50,
) -> list[tuple[str, str]]:
    if not question_sql_pairs or max_expanded_pairs <= 0:
        return []
    schema_context = _resolve_schema_expansion_context(live_schema, ddl_tables)
    if not schema_context:
        return []

    available_years = _extract_years_from_question_pairs(question_sql_pairs) or [
        2024,
        2025,
    ]
    available_cities = _expansion_city_terms()
    available_dimensions = _dedupe_keep_order(
        [
            dimension
            for question, sql in question_sql_pairs
            for dimension in _question_sql_metadata(question, sql).get("dimensions", [])
            if dimension in {item["name"] for item in schema_context["dimensions"]}
        ]
    )
    if not available_dimensions:
        available_dimensions = [item["name"] for item in schema_context["dimensions"]]

    original_signatures = {
        f"{_normalize_training_question(question).lower()}\n{re.sub(r'\s+', ' ', sql.strip()).lower()}"
        for question, sql in question_sql_pairs
    }
    expanded_pairs: list[tuple[str, str]] = []
    seen_signatures: set[str] = set()

    def add_pair(question: str, sql: str) -> bool:
        if len(expanded_pairs) >= max_expanded_pairs:
            return False
        normalized_question = _normalize_training_question(question)
        normalized_sql = re.sub(r"\s+", " ", sql.strip())
        signature = f"{normalized_question.lower()}\n{normalized_sql.lower()}"
        if (
            not normalized_question
            or not normalized_sql
            or signature in original_signatures
            or signature in seen_signatures
        ):
            return True
        seen_signatures.add(signature)
        expanded_pairs.append((normalized_question, normalized_sql))
        return len(expanded_pairs) < max_expanded_pairs

    dimension_specs = {
        item["name"]: item
        for item in schema_context["dimensions"]
        if item["name"] in available_dimensions
    }

    for dimension_name, dimension_spec in dimension_specs.items():
        for year in available_years:
            for collection_type in schema_context["collection_types"]:
                for metric in schema_context["metrics"]:
                    if not add_pair(
                        _build_schema_driven_question(
                            schema_context=schema_context,
                            city_filter=None,
                            year=year,
                            collection_type=collection_type,
                            metric=metric,
                            dimension=dimension_spec,
                        ),
                        _build_schema_driven_sql(
                            schema_context=schema_context,
                            city_filter=None,
                            year=year,
                            collection_type=collection_type,
                            metric=metric,
                            dimension=dimension_spec,
                        ),
                    ):
                        break
                if len(expanded_pairs) >= max_expanded_pairs:
                    break
            if len(expanded_pairs) >= max_expanded_pairs:
                break
        if len(expanded_pairs) >= max_expanded_pairs:
            break

    for city in available_cities:
        if len(expanded_pairs) >= max_expanded_pairs:
            break
        for dimension_name, dimension_spec in dimension_specs.items():
            if not dimension_spec.get("supports_city_filter"):
                continue
            for year in available_years:
                for collection_type in schema_context["collection_types"]:
                    for metric in schema_context["metrics"]:
                        if not add_pair(
                            _build_schema_driven_question(
                                schema_context=schema_context,
                                city_filter=city,
                                year=year,
                                collection_type=collection_type,
                                metric=metric,
                                dimension=dimension_spec,
                            ),
                            _build_schema_driven_sql(
                                schema_context=schema_context,
                                city_filter=city,
                                year=year,
                                collection_type=collection_type,
                                metric=metric,
                                dimension=dimension_spec,
                            ),
                        ):
                            break
                    if len(expanded_pairs) >= max_expanded_pairs:
                        break
                if len(expanded_pairs) >= max_expanded_pairs:
                    break
            if len(expanded_pairs) >= max_expanded_pairs:
                break

    return expanded_pairs


def _extract_city_filter_from_question(question: str) -> Optional[str]:
    normalized = _normalize_training_question(question)
    for city in _expansion_city_terms():
        if city in normalized:
            return city
    return None


def _extract_year_from_question(question: str) -> Optional[int]:
    match = re.search(r"\b((?:19|20)\d{2})年\b", question)
    if match:
        return int(match.group(1))
    return None


def _build_expanded_question_sample_records(
    expanded_question_pairs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for question, sql in expanded_question_pairs:
        metadata = _question_sql_metadata(question, sql)
        filters = metadata.get("filters", []) or []
        collection_type = (
            "全血" if "全血" in filters else "成分血" if "成分血" in filters else ""
        )
        record = {
            "question": question,
            "sql": re.sub(r"\s+", " ", sql.strip()),
            "dimension": (metadata.get("dimensions") or [""])[0],
            "metric": metadata.get("metric", ""),
            "time_expression": metadata.get("time_expression", ""),
            "collection_type": collection_type,
            "collection_type_code": "0"
            if collection_type == "全血"
            else "1"
            if collection_type == "成分血"
            else "",
            "city_filter": _extract_city_filter_from_question(question) or "",
            "year": _extract_year_from_question(question) or "",
            "table_names": metadata.get("sql_tables", []),
            "filter_tags": filters,
            "generated_at": _now_iso(),
        }
        records.append(record)
    return records


def _write_auto_question_expansions(
    expanded_question_pairs: list[tuple[str, str]],
) -> None:
    payload = {
        "generated_at": _now_iso(),
        "count": len(expanded_question_pairs),
        "items": _build_expanded_question_sample_records(expanded_question_pairs),
    }
    _write_json_file(settings.auto_question_expansions_path, payload)


def _render_analogy_rule_memory(
    question: str, sql: str, metadata: dict[str, Any]
) -> str:
    metric = str(metadata.get("metric") or "未识别")
    filters = metadata.get("filters", []) or []
    dimensions = metadata.get("dimensions", []) or []
    pattern = _generalize_question_pattern(question, metadata)
    sql_template = _parameterize_sql_template(sql)
    type_rule = "问题中的“全血/成分血”决定 BCType 过滤，不决定聚合函数。"
    metric_rule = (
        "问题中的“采集人次”对应 COUNT(*)。"
        if metric == "采集人次"
        else "问题中的“采集量”对应 SUM(a.BCPVolume)。"
        if metric == "采集量"
        else "需根据问题中的指标词决定聚合函数。"
    )
    return (
        "中文问题类比泛化规则:\n"
        f"原问题: {question}\n"
        f"泛化问法: {pattern}\n"
        f"维度: {'、'.join(dimensions) if dimensions else '未识别'}\n"
        f"指标: {metric}\n"
        f"过滤条件: {'、'.join(filters) if filters else '无显式过滤'}\n"
        f"类比规则: {type_rule}{metric_rule}\n"
        "SQL 模板:\n"
        f"{sql_template}"
    )


def _render_question_template_memories(
    question_sql_pairs: list[tuple[str, str]],
) -> list[str]:
    memories: list[str] = []
    for question, sql in question_sql_pairs:
        templates = _question_templates(question)
        memories.append(
            "中文问题模板:\n"
            f"原问题: {question}\n"
            f"模板问法: {'；'.join(templates)}\n"
            "对应 SQL 模式:\n"
            f"{sql}"
        )
    return memories


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


def _render_structured_question_memory(
    question: str, sql: str, metadata: dict[str, Any]
) -> str:
    dimensions = metadata.get("dimensions", [])
    metric = metadata.get("metric", "未识别")
    time_expression = metadata.get("time_expression", "未显式说明")
    filters = metadata.get("filters", [])
    return (
        "领域问句结构化规则:\n"
        f"原问题: {question}\n"
        f"维度: {'、'.join(dimensions) if dimensions else '未识别'}\n"
        f"指标: {metric}\n"
        f"时间: {time_expression}\n"
        f"过滤条件: {'、'.join(filters) if filters else '无显式过滤'}\n"
        "对应 SQL 模式:\n"
        f"{sql}"
    )


def _render_time_rule_memories(question_sql_pairs: list[tuple[str, str]]) -> list[str]:
    memories: list[str] = []

    for rule in TIME_EXPRESSION_RULES:
        memories.append(
            "中文时间表达规则:\n"
            f"时间短语: {rule['phrase']}\n"
            f"常见别名: {'、'.join(rule['aliases'])}\n"
            f"SQL 时间提示: {rule['hint']}"
        )

    for question, sql in question_sql_pairs:
        time_expression = _extract_time_expression(question, sql)
        if time_expression == "未显式说明":
            continue
        range_match = re.findall(r"'(\d{4}-\d{2}-\d{2})'", sql)
        sql_range = (
            " ~ ".join(range_match[:2]) if len(range_match) >= 2 else "需结合业务计算"
        )
        memories.append(
            "中文时间表达示例:\n"
            f"问题: {question}\n"
            f"时间识别: {time_expression}\n"
            f"SQL 时间范围示例: {sql_range}"
        )

    return memories


def _render_domain_rule_memories(
    business_tables: list[dict], ddl_tables: dict[str, dict]
) -> list[str]:
    memories: list[str] = []

    for rule in DOMAIN_SYNONYM_RULES:
        memories.append(
            "领域同义词规则:\n"
            f"标准术语: {rule['term']}\n"
            f"常见同义词: {'、'.join(rule['aliases'])}\n"
            f"检索提示: {rule['hint']}"
        )

    for rule in DOMAIN_METRIC_RULES:
        memories.append(
            "领域指标规则:\n"
            f"业务指标: {rule['metric']}\n"
            f"常见叫法: {'、'.join(rule['aliases'])}\n"
            f"SQL 提示: {rule['sql_hint']}"
        )

    for business_table in business_tables:
        table_name = business_table.get("table_name", "")
        ddl_table = ddl_tables.get(table_name, {})
        for field in business_table.get("fields", []):
            field_name = field.get("field_name", "")
            business_desc = field.get("description", "")
            ddl_desc = (
                ddl_table.get("columns", {}).get(field_name, {}).get("description", "")
                if ddl_table
                else ""
            )
            merged_desc = business_desc or ddl_desc
            enum_pairs = _extract_enum_mappings(merged_desc)
            if not enum_pairs:
                continue

            enum_texts = []
            for code, label in enum_pairs:
                aliases = _enum_value_aliases(label)
                enum_texts.append(f"{code}={label}（同义词: {'、'.join(aliases)}）")

            memories.append(
                "字段枚举语义规则:\n"
                f"表: {table_name}\n"
                f"字段: {field_name}\n"
                f"字段说明: {merged_desc}\n"
                f"取值规则: {'；'.join(enum_texts)}"
            )

    return memories


def _split_markdown_sections(content: str, max_chars: int = 1200) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    in_code_block = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block

        if not in_code_block and stripped.startswith("#") and current:
            section = "\n".join(current).strip()
            if section:
                sections.append(section)
            current = [line]
            continue

        current.append(line)

    if current:
        section = "\n".join(current).strip()
        if section:
            sections.append(section)

    chunks: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            chunks.append(section)
            continue

        paragraphs = [part.strip() for part in section.split("\n\n") if part.strip()]
        buffer = ""
        for paragraph in paragraphs:
            candidate = paragraph if not buffer else f"{buffer}\n\n{paragraph}"
            if len(candidate) <= max_chars:
                buffer = candidate
                continue

            if buffer:
                chunks.append(buffer)
            buffer = paragraph

        if buffer:
            chunks.append(buffer)

    return chunks


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


def _extract_question_sql_pairs(content: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    current_question: Optional[str] = None
    collecting_sql = False
    sql_lines: list[str] = []

    for line in content.splitlines():
        stripped = line.strip()

        if stripped.startswith("###") and "问题" in stripped:
            heading_match = re.match(
                r"^#{1,6}\s*问题(?:\s*\d+)?\s*[:：]?\s*(.+?)\s*$",
                stripped,
                re.IGNORECASE,
            )
            if heading_match:
                question_part = heading_match.group(1)
            else:
                _, _, question_part = stripped.partition("问题")
            current_question = _normalize_training_question(question_part)
            collecting_sql = False
            sql_lines = []
            continue

        if current_question and stripped.lower().startswith("```sql"):
            collecting_sql = True
            sql_lines = []
            continue

        if current_question and collecting_sql and stripped.startswith("```"):
            sql = "\n".join(sql_lines).strip()
            if current_question and sql:
                pairs.append((current_question, sql))
            current_question = None
            collecting_sql = False
            sql_lines = []
            continue

        if collecting_sql:
            sql_lines.append(line)

    return pairs


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


def _extract_named_value(text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}:\s*(.+)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


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


async def _train_markdown_chunks(
    knowledge_memory,
    ctx,
    content: str,
    *,
    source_type: str,
    confidence: int,
    index_records: list[dict[str, Any]],
) -> int:
    chunks = _split_markdown_sections(content)
    for chunk in chunks:
        await _save_training_text(
            knowledge_memory,
            chunk,
            ctx,
            index_records,
            source_type=source_type,
            confidence=confidence,
        )
    return len(chunks)


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


async def _train_question_rule_memories(
    knowledge_memory,
    ctx,
    question_pairs: list[tuple[str, str]],
    index_records: list[dict[str, Any]],
) -> None:
    for memory_text in _render_question_template_memories(question_pairs):
        await _save_training_text(
            knowledge_memory,
            memory_text,
            ctx,
            index_records,
            source_type="question_template",
            confidence=89,
        )

    for question, sql in question_pairs:
        metadata = _question_sql_metadata(question, sql)
        structured_text = _render_structured_question_memory(question, sql, metadata)
        await _save_training_text(
            knowledge_memory,
            structured_text,
            ctx,
            index_records,
            source_type="structured_question",
            table_names=metadata["sql_tables"],
            aliases=_question_aliases(question),
            metric_tags=_metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=_time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            confidence=92,
        )
        analogy_text = _render_analogy_rule_memory(question, sql, metadata)
        await _save_training_text(
            knowledge_memory,
            analogy_text,
            ctx,
            index_records,
            source_type="analogy_rule",
            table_names=metadata["sql_tables"],
            aliases=_question_aliases(question),
            metric_tags=_metric_tags_from_metadata(metadata),
            dimension_tags=metadata["dimensions"],
            time_tags=_time_tags_from_metadata(metadata),
            filter_tags=metadata["filters"],
            confidence=91,
        )

    for memory_text in _render_time_rule_memories(question_pairs):
        await _save_training_text(
            knowledge_memory,
            memory_text,
            ctx,
            index_records,
            source_type="time_rule",
            confidence=85,
        )


async def _train_ddl_reference_memories(
    knowledge_memory,
    ctx,
    ddl_tables: dict[str, dict[str, Any]],
    index_records: list[dict[str, Any]],
) -> None:
    for info in ddl_tables.values():
        table_name = str(info.get("table_name", ""))
        if not _should_include_table(table_name):
            continue
        table_aliases = _table_aliases(
            table_name,
            info.get("business_meaning", ""),
            info.get("table_description", ""),
        )
        if table_aliases:
            await _save_training_text(
                knowledge_memory,
                "DDL 中文映射:\n"
                f"表英文名: {table_name}\n"
                f"表中文别名: {'、'.join(table_aliases)}",
                ctx,
                index_records,
                source_type="table_alias",
                table_names=[table_name],
                aliases=table_aliases,
                confidence=78,
            )

        for field_name, field_info in info.get("columns", {}).items():
            description = field_info.get("description", "")
            aliases = _field_aliases(description) if description else []
            enum_pairs = _extract_enum_mappings(description)
            enum_labels = [label for _, label in enum_pairs]
            if aliases or enum_pairs:
                await _save_training_text(
                    knowledge_memory,
                    "DDL 字段中文映射:\n"
                    f"表: {table_name}\n"
                    f"字段: {field_name}\n"
                    f"字段说明: {description}\n"
                    f"中文别名: {'、'.join(aliases) if aliases else '无'}",
                    ctx,
                    index_records,
                    source_type="field_alias",
                    table_names=[table_name],
                    field_names=[field_name],
                    aliases=aliases,
                    enum_values=enum_labels,
                    confidence=76,
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


def _hash_live_schema(live_schema: dict[str, dict[str, Any]]) -> str:
    serialized = json.dumps(live_schema, ensure_ascii=False, sort_keys=True)
    return _hash_text(serialized)


def _build_training_fingerprint(
    *,
    include_samples: bool,
    sample_rows: int,
    business_doc_path: Optional[str],
    ddl_doc_path: Optional[str],
    live_schema_hash: str,
) -> dict[str, Any]:
    return {
        "version": TRAINING_FINGERPRINT_VERSION,
        "include_samples": bool(include_samples),
        "sample_rows": int(sample_rows),
        "business_doc_path": str(Path(business_doc_path).resolve())
        if business_doc_path
        else None,
        "business_doc_hash": _hash_file(business_doc_path),
        "ddl_doc_path": str(Path(ddl_doc_path).resolve()) if ddl_doc_path else None,
        "ddl_doc_hash": _hash_file(ddl_doc_path),
        "live_schema_hash": live_schema_hash,
        "eval_set_hash": _hash_file(settings.eval_set_path),
        "feedback_examples_hash": _hash_file(settings.feedback_examples_path),
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
        settings.auto_question_expansions_path,
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
        "table_count": 0,
        "column_count": 0,
        "knowledge_records": 0,
        "memory_records": 0,
        "knowledge_records_by_type": {},
        "memory_records_by_type": {},
        "structure_records": 0,
        "deduped_records_removed": 0,
        "business_doc_chunks": 0,
        "ddl_doc_chunks": 0,
        "question_sql_examples": 0,
        "auto_expanded_question_sql_examples": 0,
        "question_sql_examples_trained": 0,
        "auto_question_expansions_path": settings.auto_question_expansions_path,
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
    business_doc_path: Optional[str],
    ddl_doc_path: Optional[str],
    include_samples: bool,
    sample_rows: int,
    index_records: list[dict[str, Any]],
    report: dict[str, Any],
    fingerprint: dict[str, Any],
) -> None:
    manifest = {
        "generated_at": _now_iso(),
        "business_doc_path": business_doc_path,
        "ddl_doc_path": ddl_doc_path,
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
            "auto_expanded_question_sql_examples": report.get(
                "auto_expanded_question_sql_examples", 0
            ),
            "question_sql_examples_trained": report.get(
                "question_sql_examples_trained", 0
            ),
            "auto_question_expansions_path": report.get(
                "auto_question_expansions_path", settings.auto_question_expansions_path
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


def _feedback_example_is_eligible(payload: dict[str, Any]) -> tuple[bool, str]:
    user_validated = bool(payload.get("user_validated")) or (
        str(payload.get("capture_source") or "") == "online_validation"
    )
    feedback_tier = str(payload.get("feedback_tier") or "").lower()
    if feedback_tier != "gold" and not user_validated:
        return False, "not_gold_or_user_validated"
    approved = payload.get("approved") is not False
    quality_score = int(payload.get("quality_score", 0) or 0)
    if user_validated and approved and quality_score >= settings.feedback_min_quality_score:
        return True, ""
    if settings.feedback_require_execution_success and not payload.get(
        "execution_succeeded", True
    ):
        return False, "execution_failed"
    row_count = int(
        payload.get("result_row_count", settings.feedback_min_result_rows)
        or settings.feedback_min_result_rows
    )
    if (
        settings.feedback_require_nonempty_result
        and row_count < settings.feedback_min_result_rows
    ):
        return False, "result_too_small"
    default_quality_score = (
        50
        + (25 if payload.get("execution_succeeded", True) else 0)
        + (15 if row_count >= settings.feedback_min_result_rows else 0)
        + (10 if approved else 0)
    )
    quality_score = int(
        payload.get("quality_score", default_quality_score) or default_quality_score
    )
    if (
        payload.get("user_validated")
        and approved
        and quality_score >= settings.feedback_min_quality_score
    ):
        return True, ""
    if quality_score < settings.feedback_min_quality_score:
        return False, "quality_score_too_low"
    if not approved:
        return False, "not_approved"
    return True, ""


def _load_feedback_examples() -> list[dict[str, Any]]:
    path = Path(settings.feedback_examples_path)
    if not path.exists():
        return []
    examples: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("跳过损坏的反馈样本: %s", line[:80])
            continue
        question = re.sub(r"\s+", " ", str(payload.get("question") or "")).strip()
        sql = re.sub(r"\s+", " ", str(payload.get("sql") or "")).strip()
        if not question or not sql:
            continue
        signature = f"{question.lower()}\n{sql.lower()}"
        if signature in seen_signatures:
            continue
        eligible, _ = _feedback_example_is_eligible(payload)
        if not eligible:
            continue
        seen_signatures.add(signature)
        payload["question"] = question
        payload["sql"] = sql
        payload["candidate_tables"] = _dedupe_keep_order(
            [str(item) for item in payload.get("candidate_tables", []) or []]
        )
        examples.append(payload)
    return examples


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


async def _run_evaluation_suite() -> list[dict[str, Any]]:
    path = Path(settings.eval_set_path)
    if not path.exists():
        return []
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("评测集读取失败: %s", exc)
        return []
    if not isinstance(cases, list):
        logger.warning("评测集格式无效，需为数组")
        return []

    from .sql_service import generate_sql_with_feedback

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
    business_doc_path: Optional[str] = None,
    ddl_doc_path: Optional[str] = None,
) -> None:
    """增强版训练入口"""
    doc_path = business_doc_path or settings.business_doc_path
    ddl_doc_path = ddl_doc_path or settings.ddl_doc_path
    asyncio.run(_train_async(include_samples, sample_rows, doc_path, ddl_doc_path))


async def _train_async(
    include_samples: bool,
    sample_rows: int,
    business_doc_path: Optional[str],
    ddl_doc_path: Optional[str],
) -> None:
    logger.info("=== 开始增强版知识训练 ===")
    live_schema = await get_live_schema(force_refresh=True)
    fingerprint = _build_training_fingerprint(
        include_samples=include_samples,
        sample_rows=sample_rows,
        business_doc_path=business_doc_path,
        ddl_doc_path=ddl_doc_path,
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
    await _train_business_documentation(
        knowledge_memory,
        ctx,
        business_doc_path,
        ddl_doc_path,
        live_schema,
        index_records,
        report,
    )

    # 5. 加载运行期反馈样本
    await _train_feedback_examples(knowledge_memory, ctx, index_records, report)

    index_records, removed_count = _dedupe_index_records(index_records)
    report["deduped_records_removed"] = removed_count
    _flush_knowledge_index(index_records)
    reset_schema_cache()
    report = _finalize_training_report(report, index_records)
    evaluations = await _run_evaluation_suite()
    report["evaluations"] = evaluations
    report["evaluation_summary"] = {
        "total": len(evaluations),
        "passed": sum(1 for item in evaluations if item.get("passed")),
        "failed": sum(1 for item in evaluations if not item.get("passed")),
    }
    _write_training_manifest(
        business_doc_path=business_doc_path,
        ddl_doc_path=ddl_doc_path,
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
    feedback_examples = _load_feedback_examples()
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


async def _train_business_documentation(
    knowledge_memory,
    ctx,
    business_doc_path: Optional[str],
    ddl_doc_path: Optional[str],
    live_schema: dict[str, dict[str, Any]],
    index_records,
    report,
):
    logger.info("正在加载业务文档...")
    business_tables: list[dict] = []
    ddl_tables: dict[str, dict] = {}
    ddl_content = ""

    if ddl_doc_path:
        path = Path(ddl_doc_path)
        if path.exists():
            ddl_content = path.read_text(encoding="utf-8")
            ddl_tables = _extract_ddl_table_defs(ddl_content)

    if business_doc_path:
        path = Path(business_doc_path)
        if path.exists():
            business_content = path.read_text(encoding="utf-8")
            report["business_doc_chunks"] = await _train_markdown_chunks(
                knowledge_memory,
                ctx,
                business_content,
                source_type="business_doc_chunk",
                confidence=40,
                index_records=index_records,
            )

            question_pairs = _extract_question_sql_pairs(business_content)
            expanded_question_pairs = _generate_expanded_question_sql_pairs(
                question_pairs,
                live_schema,
                ddl_tables,
                max_expanded_pairs=50,
            )
            all_question_pairs = question_pairs + expanded_question_pairs
            _write_auto_question_expansions(expanded_question_pairs)
            report["question_sql_examples"] = len(question_pairs)
            report["auto_expanded_question_sql_examples"] = len(expanded_question_pairs)
            await _train_question_sql_examples(
                knowledge_memory,
                ctx,
                question_pairs,  # 只用原始样本
                index_records,
                report,
            )

            logger.info(f"业务文档扩增样本数: {len(all_question_pairs)}")

            await _train_question_rule_memories(
                knowledge_memory,
                ctx,
                all_question_pairs,  # 规则训练保留扩增样本
                index_records,
            )

            business_tables = _extract_business_table_defs(business_content)
        else:
            report["warnings"].append(f"业务文档不存在: {business_doc_path}")

    if ddl_doc_path:
        path = Path(ddl_doc_path)
        if path.exists():
            report["ddl_doc_chunks"] = await _train_markdown_chunks(
                knowledge_memory,
                ctx,
                ddl_content,
                source_type="ddl_chunk",
                confidence=45,
                index_records=index_records,
            )
            await _train_ddl_reference_memories(
                knowledge_memory,
                ctx,
                ddl_tables,
                index_records,
            )
        else:
            report["warnings"].append(f"DDL 文档不存在: {ddl_doc_path}")

    combined_alias_memories = _render_alias_memories(business_tables, ddl_tables)
    for memory_text in combined_alias_memories:
        table_name = _extract_named_value(
            memory_text, "表英文名"
        ) or _extract_named_value(memory_text, "表")
        field_name = _extract_named_value(memory_text, "字段")
        alias_line = _extract_named_value(memory_text, "中文别名")
        if table_name and not _should_include_table(table_name):
            continue
        await _save_training_text(
            knowledge_memory,
            memory_text,
            ctx,
            index_records,
            source_type="alias_dict",
            table_names=[table_name] if table_name else None,
            field_names=[field_name] if field_name else None,
            aliases=alias_line.split("、") if alias_line else None,
            confidence=84,
        )

    domain_rule_memories = _render_domain_rule_memories(business_tables, ddl_tables)
    for memory_text in domain_rule_memories:
        source_type = "metric_rule" if "指标规则" in memory_text else "synonym_rule"
        table_name = _extract_named_value(memory_text, "表")
        field_name = _extract_named_value(memory_text, "字段")
        if table_name and not _should_include_table(table_name):
            continue
        enum_values: list[str] = []
        if "取值规则:" in memory_text:
            enum_values = [
                segment.split("=", 1)[-1].split("（", 1)[0].strip()
                for segment in _extract_named_value(memory_text, "取值规则").split("；")
                if segment.strip()
            ]
        await _save_training_text(
            knowledge_memory,
            memory_text,
            ctx,
            index_records,
            source_type=source_type,
            table_names=[table_name] if table_name else None,
            field_names=[field_name] if field_name else None,
            aliases=enum_values if source_type == "synonym_rule" else None,
            enum_values=enum_values or None,
            confidence=86 if source_type == "metric_rule" else 82,
        )


async def _run_query(sql_runner, sql: str, ctx):
    args = RunSqlToolArgs(sql=sql)
    return await sql_runner.run_sql(args, ctx)
