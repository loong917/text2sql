"""Lossless tabular result admission shared by execution, review and evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd


def validate_column_names(columns: Sequence[Any]) -> list[str]:
    """Normalize scalar labels only after checking their JSON-key identity.

    SQL Server identifiers are treated case-insensitively. Numeric driver
    labels are supported, but e.g. ``1`` and ``"1"`` must never collapse into
    one row-object key. A single unnamed output is lossless; two are not.
    """
    if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes, bytearray)):
        raise ValueError("SQL result column metadata must be a sequence of labels")
    names: list[str] = []
    seen: set[str] = set()
    for column in columns:
        if isinstance(column, bool) or not isinstance(column, (str, int)):
            raise ValueError("SQL result column labels must be strings or integers")
        label = str(column)
        identity = label.casefold()
        if identity in seen:
            raise ValueError("SQL result columns must be unique (case-insensitive)")
        seen.add(identity)
        names.append(label)
    return names


@dataclass(frozen=True)
class TabularResult:
    columns: list[str]
    rows: list[dict[str, Any]]


def read_tabular_result(
    value: Any, *, declared_columns: Sequence[Any] | None = None
) -> TabularResult:
    """Validate labels before DataFrame conversion can discard duplicate columns."""
    if isinstance(value, pd.DataFrame):
        raw_columns = list(value.columns)
        columns = validate_column_names(raw_columns)
        # Only a validated, injective rename may precede row-object conversion.
        frame = value.copy(deep=False)
        frame.columns = columns
        rows = [dict(item) for item in frame.to_dict(orient="records")]
    elif isinstance(value, list) and all(isinstance(row, dict) for row in value):
        raw_columns = list(value[0]) if value else []
        columns = validate_column_names(raw_columns)
        rows = []
        for row in value:
            labels = validate_column_names(list(row))
            if set(labels) != set(columns):
                raise ValueError("SQL result rows must have the same columns")
            rows.append({str(key): item for key, item in row.items()})
    else:
        raise ValueError("SQL result must be a DataFrame or a list of row objects")
    if declared_columns is not None:
        declared = validate_column_names(declared_columns)
        if columns and columns != declared:
            raise ValueError("SQL result columns disagree with declared column metadata")
        if not columns and rows:
            raise ValueError("SQL result rows cannot omit declared columns")
        columns = declared
        if any(set(row) != set(columns) for row in rows):
            raise ValueError("SQL result rows disagree with declared column metadata")
    return TabularResult(columns, rows)
