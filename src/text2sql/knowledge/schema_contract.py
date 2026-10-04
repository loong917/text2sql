"""Validate authoritative Schema without repairing or inventing physical metadata."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, TypeGuard, cast


class SchemaContractError(ValueError):
    """A Schema is incomplete, ambiguous, or internally inconsistent."""


def _nonempty_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _integer(value: object, minimum: int, maximum: int | None = None) -> TypeGuard[int]:
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def _column_list(
    value: object, path: str, columns: Mapping[str, Any], errors: list[str], *, empty: bool
) -> list[str]:
    if not isinstance(value, list) or (not empty and not value):
        errors.append(f"{path}: must be {'a' if empty else 'a nonempty'} column list")
        return []
    seen: set[str] = set()
    valid: list[str] = []
    for column in value:
        if not _nonempty_string(column) or column not in columns:
            errors.append(f"{path}: unknown or invalid column reference")
        elif column.lower() in seen:
            errors.append(f"{path}: duplicate column reference")
        else:
            seen.add(column.lower())
            valid.append(column)
    return valid


def _validate_column(info: dict[str, Any], path: str, errors: list[str]) -> None:
    if not _nonempty_string(info.get("data_type")):
        errors.append(f"{path}.data_type: nonempty SQL data type is required")
    if type(info.get("is_nullable")) is not bool:
        errors.append(f"{path}.is_nullable: explicit boolean is required")
    for name in ("is_identity", "is_computed"):
        if name in info and type(info[name]) is not bool:
            errors.append(f"{path}.{name}: must be a boolean")
    for name, minimum, maximum in (
        ("max_length", -1, 32767),
        ("precision", 0, 255),
        ("scale", 0, 255),
    ):
        if name in info and not _integer(info[name], minimum, maximum):
            errors.append(f"{path}.{name}: invalid SQL Server metadata integer")
    if "collation_name" in info and info["collation_name"] is not None:
        if not _nonempty_string(info["collation_name"]):
            errors.append(f"{path}.collation_name: must be null or a nonempty string")
    if info.get("is_identity") is True and info.get("is_computed") is True:
        errors.append(f"{path}: a column cannot be both identity and computed")
    if str(info.get("data_type", "")).lower() in {"decimal", "numeric"}:
        precision, scale = info.get("precision"), info.get("scale")
        if precision is not None and not _integer(precision, 1, 38):
            errors.append(f"{path}.precision: decimal precision must be between 1 and 38")
        if _integer(precision, 1, 38) and _integer(scale, 0) and scale > precision:
            errors.append(f"{path}.scale: decimal scale must not exceed precision")


def _validate_foreign_keys(
    table: str, info: dict[str, Any], schema: dict[str, Any], errors: list[str]
) -> None:
    path = f"tables.{table}.foreign_keys"
    foreign_keys = info.get("foreign_keys")
    if not isinstance(foreign_keys, list):
        errors.append(f"{path}: explicit list is required (use [] when absent)")
        return
    columns = info.get("columns")
    columns = columns if isinstance(columns, dict) else {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, fk in enumerate(foreign_keys):
        entry_path = f"{path}[{index}]"
        if not isinstance(fk, dict):
            errors.append(f"{entry_path}: must be an object")
            continue
        for name in ("column_name", "referenced_table", "referenced_column", "constraint_name"):
            if not _nonempty_string(fk.get(name)):
                errors.append(f"{entry_path}.{name}: nonempty string is required")
        if not _integer(fk.get("ordinal"), 1):
            errors.append(f"{entry_path}.ordinal: positive integer is required")
        for name in ("is_disabled", "is_not_trusted"):
            if type(fk.get(name)) is not bool:
                errors.append(f"{entry_path}.{name}: explicit boolean is required")
        if not isinstance(fk.get("column_name"), str) or fk["column_name"] not in columns:
            errors.append(f"{entry_path}.column_name: unknown source column")
        target = fk.get("referenced_table")
        target_info = schema.get(target) if isinstance(target, str) else None
        if not isinstance(target_info, dict):
            errors.append(f"{entry_path}.referenced_table: unknown referenced table")
        else:
            target_columns = target_info.get("columns")
            target_columns = target_columns if isinstance(target_columns, dict) else {}
            if (
                not isinstance(fk.get("referenced_column"), str)
                or fk["referenced_column"] not in target_columns
            ):
                errors.append(f"{entry_path}.referenced_column: unknown referenced column")
        if _nonempty_string(fk.get("constraint_name")):
            grouped.setdefault(fk["constraint_name"].lower(), []).append(fk)
    for name, entries in grouped.items():
        group_path = f"{path}.{name}"
        ordinals = [entry.get("ordinal") for entry in entries]
        valid_ordinals = [value for value in ordinals if _integer(value, 1)]
        if len(valid_ordinals) != len(ordinals) or sorted(valid_ordinals) != list(
            range(1, len(entries) + 1)
        ):
            errors.append(f"{group_path}: constraint ordinals must be unique and contiguous")
        for field in ("constraint_name", "referenced_table", "is_disabled", "is_not_trusted"):
            if any(entry.get(field) != entries[0].get(field) for entry in entries):
                errors.append(f"{group_path}: inconsistent composite constraint {field}")
        for field in ("column_name", "referenced_column"):
            values = [entry.get(field) for entry in entries]
            if any(not isinstance(value, str) for value in values) or len(set(values)) != len(
                values
            ):
                errors.append(f"{group_path}: duplicate or invalid {field}")
        target = entries[0].get("referenced_table")
        target_info = schema.get(target) if isinstance(target, str) else None
        if isinstance(target_info, dict) and isinstance(target_info.get("unique_keys"), dict):
            referenced = [entry.get("referenced_column") for entry in entries]
            keys = target_info["unique_keys"].values()
            if all(isinstance(column, str) for column in referenced) and not any(
                isinstance(key, list)
                and len(key) == len(referenced)
                and set(key) == set(referenced)
                for key in keys
                if isinstance(key, list) and all(isinstance(column, str) for column in key)
            ):
                errors.append(f"{group_path}: referenced columns do not form a declared unique key")


def validate_schema_contract(schema: object) -> list[str]:
    """Return all structural errors; never coerce, fill defaults, or mutate inputs.

    Tables with no physical constraints must declare ``primary_key: []``,
    ``unique_keys: {}``, and ``foreign_keys: []``. Missing metadata is not evidence
    that a constraint is absent. Optional SQL Server column details are checked
    when present, allowing portable snapshots with the same dictionary shape.
    """
    if not isinstance(schema, dict) or not schema:
        return ["tables: nonempty Schema object is required"]
    errors: list[str] = []
    names: set[str] = set()
    object_ids: set[int] = set()
    for table, info in schema.items():
        path = f"tables.{table}"
        if not _nonempty_string(table):
            errors.append(f"{path}: nonempty table name is required")
            continue
        if table.lower() in names:
            errors.append(f"{path}: case-insensitive duplicate table identity")
        names.add(table.lower())
        if not isinstance(info, dict):
            errors.append(f"{path}: table metadata must be an object")
            continue
        schema_name = info.get("schema_name")
        qualified_name = info.get("qualified_name")
        if not _nonempty_string(schema_name):
            errors.append(f"{path}.schema_name: nonempty schema identity is required")
        else:
            if schema_name.lower() == "dbo":
                expected_name = f"{schema_name}.{table}"
                if table.lower().startswith("dbo."):
                    errors.append(f"{path}: dbo table key must be unqualified")
            else:
                expected_name = table
                if not table.startswith(f"{schema_name}.") or table == f"{schema_name}.":
                    errors.append(f"{path}: table key must include its non-dbo schema")
            if qualified_name != expected_name:
                errors.append(f"{path}.qualified_name: does not match schema/table identity")
        if not _nonempty_string(qualified_name):
            errors.append(f"{path}.qualified_name: nonempty qualified identity is required")
        if "object_id" in info:
            if not _integer(info["object_id"], 1):
                errors.append(f"{path}.object_id: positive integer is required when provided")
            elif info["object_id"] in object_ids:
                errors.append(f"{path}.object_id: duplicate physical object identity")
            else:
                object_ids.add(info["object_id"])
        columns = info.get("columns")
        if not isinstance(columns, dict) or not columns:
            errors.append(f"{path}.columns: nonempty column metadata object is required")
            columns = {}
        column_names: set[str] = set()
        for column, column_info in columns.items():
            column_path = f"{path}.columns.{column}"
            if not _nonempty_string(column):
                errors.append(f"{column_path}: nonempty column name is required")
            elif column.lower() in column_names:
                errors.append(f"{column_path}: case-insensitive duplicate column identity")
            else:
                column_names.add(column.lower())
            if not isinstance(column_info, dict):
                errors.append(f"{column_path}: column metadata must be an object")
            else:
                _validate_column(column_info, column_path, errors)
        primary = _column_list(
            info.get("primary_key"), f"{path}.primary_key", columns, errors, empty=True
        )
        unique_keys = info.get("unique_keys")
        valid_keys: list[list[str]] = []
        if not isinstance(unique_keys, dict):
            errors.append(f"{path}.unique_keys: explicit object is required (use {{}} when absent)")
        else:
            key_names: set[str] = set()
            for name, key_columns in unique_keys.items():
                if not _nonempty_string(name):
                    errors.append(f"{path}.unique_keys: nonempty constraint name is required")
                elif name.lower() in key_names:
                    errors.append(f"{path}.unique_keys.{name}: duplicate constraint identity")
                else:
                    key_names.add(name.lower())
                valid_keys.append(
                    _column_list(
                        key_columns, f"{path}.unique_keys.{name}", columns, errors, empty=False
                    )
                )
        if primary and primary not in valid_keys:
            errors.append(f"{path}.primary_key: must match a declared unique key in ordinal order")
        if any(
            columns[column].get("is_nullable") is not False
            for column in primary
            if isinstance(columns[column], dict)
        ):
            errors.append(f"{path}.primary_key: primary key columns cannot be nullable")
    for table, info in schema.items():
        if isinstance(table, str) and isinstance(info, dict):
            _validate_foreign_keys(table, info, schema, errors)
    return errors


def require_schema_contract(schema: object) -> dict[str, dict[str, Any]]:
    """Return a valid Schema unchanged, or raise with path-qualified errors."""
    errors = validate_schema_contract(schema)
    if errors:
        raise SchemaContractError("invalid authoritative Schema: " + "; ".join(errors[:20]))
    return cast(dict[str, dict[str, Any]], schema)


def usable_foreign_keys(info: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield only constraints whose enforcement and trust are explicitly known."""
    foreign_keys = info.get("foreign_keys", [])
    if not isinstance(foreign_keys, list):
        return
    for fk in foreign_keys:
        if (
            isinstance(fk, dict)
            and fk.get("is_disabled") is False
            and fk.get("is_not_trusted") is False
        ):
            yield fk


def usable_single_column_foreign_keys(info: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Only complete single-column constraints are supported by the current join IR.

    Group *all* rows first so a malformed or partially disabled composite FK
    cannot masquerade as a single-column relationship after filtering.
    """
    foreign_keys = info.get("foreign_keys", [])
    if not isinstance(foreign_keys, list):
        return
    groups: dict[str, list[dict[str, Any]]] = {}
    for fk in foreign_keys:
        if isinstance(fk, dict) and _nonempty_string(fk.get("constraint_name")):
            groups.setdefault(fk["constraint_name"].lower(), []).append(fk)
    for entries in groups.values():
        if len(entries) != 1:
            continue
        fk = entries[0]
        if (
            _integer(fk.get("ordinal"), 1, 1)
            and all(
                _nonempty_string(fk.get(field))
                for field in ("column_name", "referenced_table", "referenced_column")
            )
            and fk.get("is_disabled") is False
            and fk.get("is_not_trusted") is False
        ):
            yield fk
