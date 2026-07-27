from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TableCard:
    table_name: str
    text: str
    token_cost: int


def schema_fingerprint(schema: dict[str, dict[str, Any]]) -> str:
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_table_cards(schema: dict[str, dict[str, Any]]) -> list[TableCard]:
    cards: list[TableCard] = []
    for table_name, info in sorted(schema.items()):
        lines = [
            f"表名: {table_name}",
            f"表说明: {info.get('description') or '无'}",
            "字段:",
        ]
        for column_name, column in info.get("columns", {}).items():
            lines.append(
                f"- {column_name}; 类型={column.get('data_type', '')}; "
                f"说明={column.get('description') or '无'}"
            )
        foreign_keys = info.get("foreign_keys", []) or []
        if foreign_keys:
            lines.append("外键:")
            for fk in foreign_keys:
                lines.append(
                    f"- {table_name}.{fk.get('column_name')} -> "
                    f"{fk.get('referenced_table')}"
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


def load_schema_snapshot(index_path: str | Path) -> dict[str, dict[str, Any]]:
    """Rebuild a retrieval-only schema from the offline knowledge sidecar."""
    path = Path(index_path)
    if not path.exists():
        return {}
    records = json.loads(path.read_text(encoding="utf-8"))
    schema: dict[str, dict[str, Any]] = {}
    for record in records:
        source_type = str(record.get("source_type") or "")
        table_names = [str(item) for item in record.get("table_names", []) or []]
        if source_type == "table_description" and table_names:
            table = schema.setdefault(
                table_names[0], {"description": "", "columns": {}, "foreign_keys": []}
            )
            aliases = record.get("aliases", []) or []
            table["description"] = (
                str(aliases[0]) if aliases else str(record.get("content") or "")
            )
        elif source_type == "column_schema" and table_names:
            table = schema.setdefault(
                table_names[0], {"description": "", "columns": {}, "foreign_keys": []}
            )
            for field_name in record.get("field_names", []) or []:
                table["columns"].setdefault(
                    str(field_name), {"data_type": "", "description": ""}
                )
        elif source_type == "foreign_key" and len(table_names) >= 2:
            table = schema.setdefault(
                table_names[0], {"description": "", "columns": {}, "foreign_keys": []}
            )
            fields = record.get("field_names", []) or []
            table["foreign_keys"].append(
                {
                    "column_name": str(fields[0]) if fields else "",
                    "referenced_table": table_names[1],
                }
            )
    return schema
