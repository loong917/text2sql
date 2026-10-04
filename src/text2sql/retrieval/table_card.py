"""Create compact, deterministic semantic documents for database tables."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..knowledge.provenance import schema_fingerprint as _schema_fingerprint
from ..knowledge.schema_contract import require_schema_contract, usable_foreign_keys


@dataclass(frozen=True)
class TableCard:
    table_name: str
    text: str
    token_cost: int


def business_cards_fingerprint(records: Sequence[Mapping[str, Any]]) -> str:
    """Bind the embedding representation to reviewed business descriptions."""
    ordered = sorted((dict(item) for item in records), key=lambda item: str(item.get("table", "")))
    return hashlib.sha256(
        json.dumps(ordered, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_table_cards(
    schema: dict[str, dict[str, Any]],
    business_cards: Sequence[Mapping[str, Any]] = (),
) -> list[TableCard]:
    """Fuse reviewed business vocabulary with authoritative metadata.

    Business records enrich retrieval text but never introduce tables or columns
    that are absent from the supplied Schema snapshot.
    """
    require_schema_contract(schema)
    business_by_table = {str(item.get("table", "")).lower(): item for item in business_cards}
    cards: list[TableCard] = []
    for table_name, info in sorted(schema.items()):
        business = business_by_table.get(table_name.lower(), {})
        lines = [
            f"表名: {table_name}",
            f"物理名称: {info['qualified_name']}",
            f"表说明: {info.get('description') or '无'}",
        ]
        if business:
            lines.append(f"业务说明: {business.get('description', '')}")
            for label, key in (
                ("业务别名", "business_aliases"),
                ("指标", "metrics"),
                ("维度", "dimensions"),
            ):
                values = [str(value) for value in business.get(key, ())]
                if values:
                    lines.append(f"{label}: {'、'.join(values)}")
            important = [
                str(value)
                for value in business.get("important_columns", ())
                if value in info.get("columns", {})
            ]
            if important:
                lines.append(f"关键字段: {'、'.join(important)}")
        lines.append("字段:")
        for column_name, column in info.get("columns", {}).items():
            details = [f"类型={column['data_type']}", f"可空={column['is_nullable']}"]
            for label, key in (
                ("最大字节长度", "max_length"),
                ("精度", "precision"),
                ("小数位", "scale"),
                ("排序规则", "collation_name"),
                ("自增", "is_identity"),
                ("计算列", "is_computed"),
            ):
                if key in column and column[key] is not None:
                    details.append(f"{label}={column[key]}")
            lines.append(
                f"- {column_name}; {'; '.join(details)}; 说明={column.get('description') or '无'}"
            )
        lines.append(f"主键: {', '.join(info['primary_key']) or '无'}")
        if info["unique_keys"]:
            lines.append("唯一键:")
            for name, columns in sorted(info["unique_keys"].items()):
                lines.append(f"- {name}: ({', '.join(columns)})")
        foreign_keys = list(usable_foreign_keys(info))
        if foreign_keys:
            lines.append("已启用且受信任的外键:")
            groups: dict[str, list[dict[str, Any]]] = {}
            for fk in foreign_keys:
                groups.setdefault(fk["constraint_name"], []).append(fk)
            for name, entries in sorted(groups.items()):
                entries.sort(key=lambda entry: entry["ordinal"])
                source_columns = ", ".join(entry["column_name"] for entry in entries)
                target_columns = ", ".join(entry["referenced_column"] for entry in entries)
                lines.append(
                    f"- {name}: {table_name}.({source_columns}) -> "
                    f"{entries[0]['referenced_table']}.({target_columns})"
                )
        text = "\n".join(lines)
        cards.append(
            TableCard(
                table_name=table_name,
                text=text,
                token_cost=max(1, len(text) // 4),
            )
        )
    return cards


def load_schema_snapshot(snapshot_path: str | Path) -> dict[str, dict[str, Any]]:
    """Load and fingerprint-check the canonical offline Schema snapshot."""
    path = Path(snapshot_path)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
        raise ValueError(f"Schema 快照格式无效: {path}")
    schema = payload["tables"]
    declared = str(payload.get("schema_version") or "")
    actual = f"sha256:{_schema_fingerprint(schema)}"
    if declared != actual:
        raise ValueError(
            f"Schema 快照指纹不匹配: declared={declared or 'missing'}, actual={actual}"
        )
    return require_schema_contract(schema)
