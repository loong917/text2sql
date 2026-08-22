"""Load and validate the machine-readable Text2SQL knowledge base."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from ..domain.semantic_ir import SemanticCatalog, parse_question_semantics
from ..domain.sql_validation import validate_tsql_ast
from .models import RECORD_MODELS, KnowledgeManifest


class KnowledgeValidationError(ValueError):
    """Raised when runtime knowledge is missing, malformed, or schema-incompatible."""


@dataclass
class KnowledgeBundle:
    table_cards: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
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

    def semantic_catalog(self) -> SemanticCatalog:
        """Project validated knowledge records into the domain semantic catalog."""
        entity_policies = tuple(
            {
                **item,
                "entity": str(item.get("entity") or "blood_type"),
            }
            for item in self.policies
            if item.get("kind") == "entity_filter"
        )
        dimensions = tuple(
            {
                **item,
                "kind": (
                    "date"
                    if str(item.get("id") or "").endswith("date")
                    else item.get("kind", "dimension")
                ),
            }
            for item in self.dimensions
        )
        return SemanticCatalog(
            metrics=tuple(self.metrics),
            dimensions=dimensions,
            entity_policies=entity_policies,
            joins=tuple(self.joins),
            entities=tuple(self.entities),
        )


def _read_json(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("root must be a JSON array")
        records: list[dict[str, Any]] = []
        for index, item in enumerate(payload, 1):
            if not isinstance(item, dict):
                errors.append(f"{path}:{index}: record must be an object")
                continue
            records.append(item)
        return records
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


def _validate_record_models(
    attribute: str,
    records: list[dict[str, Any]],
    errors: list[str],
) -> list[dict[str, Any]]:
    model = RECORD_MODELS[attribute]
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(records, 1):
        try:
            validated.append(model.model_validate(item).model_dump())
        except ValidationError as exc:
            errors.append(f"{attribute}:{index}: {exc.errors(include_url=False)}")
    return validated


def load_knowledge_bundle(
    root: str | Path, live_schema: dict[str, dict[str, Any]]
) -> KnowledgeBundle:
    """Load typed knowledge, rejecting records that reference unknown schema objects."""
    root_path = Path(root)
    bundle = KnowledgeBundle()
    manifest_path = root_path / "manifest.json"
    if not manifest_path.exists():
        bundle.errors.append(f"{manifest_path}: knowledge manifest is required")
    else:
        try:
            KnowledgeManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            bundle.files.append(str(manifest_path.resolve()))
        except (OSError, ValidationError) as exc:
            bundle.errors.append(f"{manifest_path}: {exc}")
    inputs = {
        "table_cards": (root_path / "schema" / "table_cards.jsonl", _read_jsonl),
        "metrics": (root_path / "domain" / "metrics.json", _read_json),
        "dimensions": (root_path / "domain" / "dimensions.json", _read_json),
        "joins": (root_path / "domain" / "joins.json", _read_json),
        "policies": (root_path / "domain" / "policies.json", _read_json),
        "entities": (root_path / "domain" / "entities.json", _read_json),
        "gold_sql": (root_path / "examples" / "gold_sql.jsonl", _read_jsonl),
        "negative_sql": (root_path / "examples" / "negative_sql.jsonl", _read_jsonl),
        "refusals": (root_path / "examples" / "refusal.jsonl", _read_jsonl),
    }
    for attribute, (path, reader) in inputs.items():
        records = _dedupe_records(reader(path, bundle.errors), attribute, bundle.errors)
        records = _validate_record_models(attribute, records, bundle.errors)
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
            _validate_column(table, str(column), source, live_schema, names, bundle.errors)
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
                str(item.get("table") or ""), str(column), source, live_schema, names, bundle.errors
            )
            for column in columns
        ):
            valid_dimensions.append(item)
    bundle.dimensions = valid_dimensions
    valid_joins: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.joins, 1):
        source = f"joins:{index}"
        left_ok = _validate_column(
            str(item.get("left_table") or ""),
            str(item.get("left_column") or ""),
            source,
            live_schema,
            names,
            bundle.errors,
        )
        right_ok = _validate_column(
            str(item.get("right_table") or ""),
            str(item.get("right_column") or ""),
            source,
            live_schema,
            names,
            bundle.errors,
        )
        if left_ok and right_ok:
            valid_joins.append(item)
    bundle.joins = valid_joins
    valid_policies: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.policies, 1):
        source = f"policies:{index}"
        if item.get("table") and item.get("column"):
            if not _validate_column(
                str(item["table"]), str(item["column"]), source, live_schema, names, bundle.errors
            ):
                continue
        valid_policies.append(item)
    bundle.policies = valid_policies
    valid_entities: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.entities, 1):
        source = f"entities:{index}"
        if _validate_column(
            str(item.get("table") or ""),
            str(item.get("column") or ""),
            source,
            live_schema,
            names,
            bundle.errors,
        ) and isinstance(item.get("values"), dict):
            valid_entities.append(item)
    bundle.entities = valid_entities
    valid_gold: list[dict[str, Any]] = []
    catalog = bundle.semantic_catalog()
    for index, item in enumerate(bundle.gold_sql, 1):
        source = f"gold_sql:{index}"
        if str(item.get("status") or "") != "approved":
            continue
        if not _validate_sql(item, source, live_schema, names, bundle.errors):
            continue
        semantic_error = validate_tsql_ast(
            str(item.get("sql") or ""),
            live_schema,
            parse_question_semantics(str(item.get("question") or ""), catalog),
        )
        if semantic_error:
            bundle.errors.append(f"{source}: {semantic_error}")
            continue
        valid_gold.append(item)
    bundle.gold_sql = valid_gold
    return bundle


def load_validated_knowledge_bundle(
    root: str | Path,
    live_schema: dict[str, dict[str, Any]],
) -> KnowledgeBundle:
    """Load knowledge for runtime use and fail closed on every rejected record."""
    bundle = load_knowledge_bundle(root, live_schema)
    if not bundle.available:
        raise KnowledgeValidationError(f"structured knowledge is unavailable: {root}")
    if bundle.errors:
        raise KnowledgeValidationError("; ".join(bundle.errors[:12]))
    return bundle
