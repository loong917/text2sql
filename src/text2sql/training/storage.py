"""Atomic file persistence and memory writes for offline builds."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.logging import setup_logging
from .records import append_index_record

logger = setup_logging("text2sql.training.storage")


def flush_knowledge_index(records: list[dict[str, Any]], index_path_value: str) -> None:
    index_path = Path(index_path_value)
    atomic_write_text(index_path, json.dumps(records, ensure_ascii=False, indent=2))
    logger.info("知识索引已写入: %s (%d 条)", index_path, len(records))


async def save_training_text(
    knowledge_memory,
    text: str,
    ctx,
    index_records: list[dict[str, Any]],
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

    await knowledge_memory.save_text_memory(content, ctx)
    append_index_record(
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


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_json_file(path_str: str, payload: Any) -> None:
    path = Path(path_str)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json_file(path_str: str) -> dict[str, Any] | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 JSON 文件失败 %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None
