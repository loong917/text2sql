"""Load and validate the machine-readable Text2SQL knowledge base."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from ..domain.semantic_contract import build_semantic_snapshot
from ..domain.semantic_ir import SemanticCatalog, parse_question_semantics
from ..domain.sql_scope import base_tables, table_key
from ..domain.sql_validation import validate_tsql_ast
from .models import RECORD_MODELS, KnowledgeManifest
from .provenance import schema_fingerprint
from .schema_contract import validate_schema_contract


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
        content = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in RECORD_MODELS}

    def semantic_catalog(self) -> SemanticCatalog:
        """Project validated knowledge records into the domain semantic catalog."""
        entity_policies = tuple(
            {
                **item,
                "entity": str(item.get("entity") or item["id"]),
            }
            for item in self.policies
            if item.get("kind") == "entity_filter"
        )
        dimensions = tuple(
            {
                **item,
                "kind": item.get("kind", "dimension"),
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


KnowledgeReader = Callable[[str | Path], bytes | None]


def _read_bytes(path: Path, reader: KnowledgeReader | None) -> bytes | None:
    if reader is not None:
        return reader(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _read_json(
    path: Path, errors: list[str], reader: KnowledgeReader | None = None
) -> list[dict[str, Any]]:
    content = _read_bytes(path, reader)
    if content is None:
        return []
    try:
        payload = json.loads(content)
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


def _read_jsonl(
    path: Path, errors: list[str], reader: KnowledgeReader | None = None
) -> list[dict[str, Any]]:
    content = _read_bytes(path, reader)
    if content is None:
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        errors.append(f"{path}: {exc}")
        return []
    for line_number, line in enumerate(lines, 1):
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
    scopes = traverse_scope(tree)
    sql_tables = {table_key(table) for table in base_tables(scopes[-1])} if scopes else set()
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
            validated.append(model.model_validate(item).model_dump(exclude_unset=True))
        except ValidationError as exc:
            errors.append(f"{attribute}:{index}: {exc.errors(include_url=False)}")
    return validated


def load_knowledge_bundle(
    root: str | Path,
    live_schema: dict[str, dict[str, Any]],
    *,
    reader: KnowledgeReader | None = None,
) -> KnowledgeBundle:
    """Load typed knowledge, rejecting records that reference unknown schema objects."""
    root_path = Path(root)
    bundle = KnowledgeBundle()
    manifest_path = root_path / "manifest.json"
    manifest_bytes = _read_bytes(manifest_path, reader)
    if manifest_bytes is None:
        bundle.errors.append(f"{manifest_path}: knowledge manifest is required")
    else:
        try:
            KnowledgeManifest.model_validate_json(manifest_bytes)
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
    for attribute, (path, record_loader) in inputs.items():
        records = _dedupe_records(
            record_loader(path, bundle.errors, reader), attribute, bundle.errors
        )
        records = _validate_record_models(attribute, records, bundle.errors)
        setattr(bundle, attribute, records)
        if _read_bytes(path, reader) is not None:
            bundle.files.append(str(path.resolve()))

    return validate_bundle_schema(bundle, live_schema)


def validate_bundle_schema(
    bundle: KnowledgeBundle, live_schema: dict[str, dict[str, Any]]
) -> KnowledgeBundle:
    """Apply the same reference and semantic checks to source and frozen knowledge."""
    schema_errors = validate_schema_contract(live_schema)
    if schema_errors:
        bundle.errors.extend(schema_errors)
        return bundle
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
            metric_table_info = live_schema[names[item["source_table"].lower()]]
            column = item["column"]
            if column != "*" and item["aggregation"] in {"SUM", "AVG"}:
                details = next(
                    value
                    for name, value in metric_table_info["columns"].items()
                    if name.lower() == column.lower()
                )
                if details["data_type"].lower() not in {
                    "tinyint",
                    "smallint",
                    "int",
                    "bigint",
                    "decimal",
                    "numeric",
                    "float",
                    "real",
                    "money",
                    "smallmoney",
                }:
                    bundle.errors.append(f"{source}: SUM/AVG require a numeric source column")
                    continue
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
            left = live_schema[names[item["left_table"].lower()]]
            right = live_schema[names[item["right_table"].lower()]]
            right_unique = any(
                [column.lower() for column in key] == [item["right_column"].lower()]
                for key in right["unique_keys"].values()
            )
            left_unique = any(
                [column.lower() for column in key] == [item["left_column"].lower()]
                for key in left["unique_keys"].values()
            )
            if not right_unique or (item["relationship"] == "one_to_one" and not left_unique):
                bundle.errors.append(f"{source}: join cardinality is not proven by unique keys")
                continue
            left_type = next(
                value["data_type"].lower()
                for column, value in left["columns"].items()
                if column.lower() == item["left_column"].lower()
            )
            right_type = next(
                value["data_type"].lower()
                for column, value in right["columns"].items()
                if column.lower() == item["right_column"].lower()
            )
            if left_type != right_type:
                bundle.errors.append(f"{source}: join endpoint data types do not match")
                continue
            valid_joins.append(item)
    bundle.joins = valid_joins
    valid_policies: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.policies, 1):
        source = f"policies:{index}"
        if item.get("kind") == "required_join" and item["join_id"] not in {
            join["id"] for join in bundle.joins
        }:
            bundle.errors.append(f"{source}: unknown or unproven join_id")
            continue
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
        if item["execution"]["schema_fingerprint"] != schema_fingerprint(live_schema):
            bundle.errors.append(f"{source}: Gold execution evidence belongs to another Schema")
            continue
        if not _validate_sql(item, source, live_schema, names, bundle.errors):
            continue
        plan = parse_question_semantics(str(item.get("question") or ""), catalog)
        if plan.date_is_relative:
            bundle.errors.append(f"{source}: Gold 问题必须使用绝对日期，不能冻结会漂移的相对时间")
            continue
        if item["semantic_ir"] != build_semantic_snapshot(plan):
            bundle.errors.append(f"{source}: authored semantic truth conflicts with grounded plan")
            continue
        semantic_error = validate_tsql_ast(
            str(item.get("sql") or ""),
            live_schema,
            plan,
        )
        if semantic_error:
            bundle.errors.append(f"{source}: {semantic_error}")
            continue
        valid_gold.append(item)
    bundle.gold_sql = valid_gold
    bundle.negative_sql = [item for item in bundle.negative_sql if item["status"] == "approved"]
    bundle.refusals = [item for item in bundle.refusals if item["status"] == "approved"]
    return bundle


def load_validated_knowledge_bundle(
    root: str | Path,
    live_schema: dict[str, dict[str, Any]],
    *,
    require_reviewed: bool = False,
    reader: KnowledgeReader | None = None,
) -> KnowledgeBundle:
    """Load knowledge for runtime use and fail closed on every rejected record."""
    bundle = load_knowledge_bundle(root, live_schema, reader=reader)
    if require_reviewed:
        validate_bundle_reviews(bundle, live_schema)
    if not bundle.available:
        raise KnowledgeValidationError(f"structured knowledge is unavailable: {root}")
    if bundle.errors:
        raise KnowledgeValidationError("; ".join(bundle.errors[:12]))
    return bundle


def validate_bundle_reviews(
    bundle: KnowledgeBundle, live_schema: dict[str, dict[str, Any]]
) -> None:
    """Enforce identical production approval requirements on sources and snapshots."""
    if not bundle.metrics:
        bundle.errors.append("production knowledge requires reviewed metric definitions")
    if {item["table"].casefold() for item in bundle.table_cards} != {
        table.casefold() for table in live_schema
    }:
        bundle.errors.append(
            "production knowledge requires a reviewed table card for every Schema table"
        )
    digest = schema_fingerprint(live_schema)
    for name in RECORD_MODELS:
        for item in getattr(bundle, name):
            review = item.get("review")
            if not review or review["schema_fingerprint"] != digest:
                bundle.errors.append(
                    f"{name}:{item.get('id', item.get('table'))}: current Schema review required"
                )
            unit = item.get("unit")
            if name == "metrics" and (
                not isinstance(unit, str)
                or not unit.strip()
                or unit != unit.strip()
                or unit.strip() == "数据库原始单位"
            ):
                bundle.errors.append(f"metrics:{item['id']}: an authoritative unit is required")
            grain = item.get("grain")
            if name == "table_cards" and (
                not isinstance(grain, str) or not grain.strip() or grain != grain.strip()
            ):
                bundle.errors.append(
                    f"table_cards:{item['table']}: reviewed fact grain is required"
                )
