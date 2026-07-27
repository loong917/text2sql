"""Load and validate the machine-readable Text2SQL knowledge base."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError


@dataclass
class KnowledgeBundle:
    table_cards: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    gold_sql: list[dict[str, Any]] = field(default_factory=list)
    negative_sql: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.files)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for name in sorted(self.files):
            path = Path(name)
            digest.update(str(path).encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest() if self.files else ""


def _read_json(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("root must be a JSON array")
        return [item for item in payload if isinstance(item, dict)]
    except Exception as exc:
        errors.append(f"{path}: {exc}")
        return []


def _read_jsonl(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("record must be an object")
            records.append(item)
        except Exception as exc:
            errors.append(f"{path}:{line_number}: {exc}")
    return records


def _schema_names(live_schema: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {name.lower(): name for name in live_schema}


def _validate_table(
    table: str, source: str, schema_names: dict[str, str], errors: list[str]
) -> bool:
    if not table or table.lower() not in schema_names:
        errors.append(f"{source}: unknown table '{table}'")
        return False
    return True


def _validate_column(
    table: str,
    column: str,
    source: str,
    live_schema: dict[str, dict[str, Any]],
    schema_names: dict[str, str],
    errors: list[str],
) -> bool:
    if not _validate_table(table, source, schema_names, errors):
        return False
    actual = schema_names[table.lower()]
    columns = {name.lower() for name in live_schema[actual].get("columns", {})}
    if column != "*" and column.lower() not in columns:
        errors.append(f"{source}: unknown column '{table}.{column}'")
        return False
    return True


def _validate_sql(
    item: dict[str, Any],
    source: str,
    live_schema: dict[str, dict[str, Any]],
    schema_names: dict[str, str],
    errors: list[str],
) -> bool:
    sql = str(item.get("sql") or "").strip()
    question = str(item.get("question") or "").strip()
    if not question or not sql:
        errors.append(f"{source}: question and sql are required")
        return False
    try:
        tree = parse_one(sql, read="tsql")
    except ParseError as exc:
        errors.append(f"{source}: invalid T-SQL: {exc}")
        return False
    if not tree.find(exp.Select) and not isinstance(tree, exp.Select):
        errors.append(f"{source}: only SELECT queries are allowed")
        return False
    valid = True
    sql_tables = {table.name for table in tree.find_all(exp.Table)}
    for table in sql_tables:
        valid = _validate_table(table, source, schema_names, errors) and valid
    declared = {str(value) for value in item.get("tables", [])}
    if declared and {value.lower() for value in declared} != {
        value.lower() for value in sql_tables
    }:
        errors.append(f"{source}: declared tables do not match SQL AST tables")
        valid = False
    return valid


def _dedupe_records(
    records: list[dict[str, Any]], source: str, errors: list[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_content: set[str] = set()
    for index, item in enumerate(records, 1):
        record_id = str(item.get("id") or "").strip()
        content = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if record_id and record_id in seen_ids:
            errors.append(f"{source}:{index}: duplicate id '{record_id}'")
            continue
        if content in seen_content:
            errors.append(f"{source}:{index}: duplicate record")
            continue
        if record_id:
            seen_ids.add(record_id)
        seen_content.add(content)
        result.append(item)
    return result


def load_knowledge_bundle(
    root: str | Path, live_schema: dict[str, dict[str, Any]]
) -> KnowledgeBundle:
    """Load typed knowledge, rejecting records that reference unknown schema objects."""
    root_path = Path(root)
    bundle = KnowledgeBundle()
    inputs = {
        "table_cards": (root_path / "schema" / "table_cards.jsonl", _read_jsonl),
        "metrics": (root_path / "domain" / "metrics.json", _read_json),
        "dimensions": (root_path / "domain" / "dimensions.json", _read_json),
        "joins": (root_path / "domain" / "joins.json", _read_json),
        "policies": (root_path / "domain" / "policies.json", _read_json),
        "gold_sql": (root_path / "examples" / "gold_sql.jsonl", _read_jsonl),
        "negative_sql": (root_path / "examples" / "negative_sql.jsonl", _read_jsonl),
        "refusals": (root_path / "examples" / "refusal.jsonl", _read_jsonl),
    }
    for attribute, (path, reader) in inputs.items():
        records = _dedupe_records(reader(path, bundle.errors), attribute, bundle.errors)
        setattr(bundle, attribute, records)
        if path.exists():
            bundle.files.append(str(path.resolve()))

    names = _schema_names(live_schema)
    valid_cards: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.table_cards, 1):
        source = f"table_cards:{index}"
        table = str(item.get("table") or "")
        if not _validate_table(table, source, names, bundle.errors):
            continue
        if all(
            _validate_column(
                table, str(column), source, live_schema, names, bundle.errors
            )
            for column in item.get("important_columns", [])
        ):
            valid_cards.append(item)
    bundle.table_cards = valid_cards
    valid_metrics: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.metrics, 1):
        source = f"metrics:{index}"
        if _validate_column(
            str(item.get("source_table") or ""),
            str(item.get("column") or item.get("expression") or ""),
            source,
            live_schema,
            names,
            bundle.errors,
        ):
            valid_metrics.append(item)
    bundle.metrics = valid_metrics
    valid_dimensions: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.dimensions, 1):
        source = f"dimensions:{index}"
        columns = item.get("columns", [])
        if columns and all(
            _validate_column(
                str(item.get("table") or ""), str(column), source,
                live_schema, names, bundle.errors
            )
            for column in columns
        ):
            valid_dimensions.append(item)
    bundle.dimensions = valid_dimensions
    valid_joins: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.joins, 1):
        source = f"joins:{index}"
        left_ok = _validate_column(
            str(item.get("left_table") or ""), str(item.get("left_column") or ""),
            source, live_schema, names, bundle.errors
        )
        right_ok = _validate_column(
            str(item.get("right_table") or ""), str(item.get("right_column") or ""),
            source, live_schema, names, bundle.errors
        )
        if left_ok and right_ok:
            valid_joins.append(item)
    bundle.joins = valid_joins
    valid_policies: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.policies, 1):
        source = f"policies:{index}"
        if item.get("table") and item.get("column"):
            if not _validate_column(
                str(item["table"]), str(item["column"]), source,
                live_schema, names, bundle.errors
            ):
                continue
        valid_policies.append(item)
    bundle.policies = valid_policies
    bundle.gold_sql = [
        item
        for index, item in enumerate(bundle.gold_sql, 1)
        if str(item.get("status") or "") == "approved"
        and _validate_sql(item, f"gold_sql:{index}", live_schema, names, bundle.errors)
    ]
    return bundle
