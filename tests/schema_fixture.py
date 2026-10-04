"""Explicitly synthetic physical metadata for isolated tests, never production data."""

from copy import deepcopy
from typing import Any


def synthetic_schema(tables: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Complete ordinary test tables without inferring primary/unique/FK relationships.

    Unspecified test column types are intentionally ``int`` and non-nullable.
    Existing foreign keys and key definitions must be declared completely by the
    calling fixture; this helper does not invent the target or cardinality.
    """
    schema = deepcopy(tables)
    for name, info in schema.items():
        schema_name = name.split(".", 1)[0] if "." in name else "dbo"
        info.setdefault("schema_name", schema_name)
        info.setdefault("qualified_name", name if "." in name else f"dbo.{name}")
        info.setdefault("primary_key", [])
        info.setdefault("unique_keys", {})
        info.setdefault("foreign_keys", [])
        for details in info["columns"].values():
            details.setdefault("data_type", "int")
            details.setdefault("is_nullable", False)
    return schema
