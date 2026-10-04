"""Compare SQL results without erasing NULLs, value types, ordering or duplicates."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import pandas as pd
from sqlglot import parse_one
from sqlglot.errors import ParseError

from ..domain.result_contract import read_tabular_result, validate_column_names


def result_rows(value: Any) -> list[dict[str, Any]]:
    return read_tabular_result(value).rows


def result_columns(value: Any) -> list[str]:
    return read_tabular_result(value).columns


def _tolerance(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


@dataclass(frozen=True)
class ResultComparison:
    mode: Literal["ordered", "bag"] = "bag"
    absolute_tolerance: Decimal = Decimal("0")
    relative_tolerance: Decimal = Decimal("0")

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> ResultComparison:
        options = case.get("result_comparison", {})
        if not isinstance(options, dict) or set(options) - {
            "mode",
            "absolute_tolerance",
            "relative_tolerance",
        }:
            raise ValueError("result_comparison contains unsupported options")
        try:
            baseline = parse_one(str(case.get("baseline_sql") or ""), read="tsql")
            ordered = baseline is not None and baseline.args.get("order") is not None
        except ParseError:
            ordered = False
        mode = options.get("mode", "ordered" if ordered else "bag")
        if not isinstance(mode, str) or mode not in {"ordered", "bag"}:
            raise ValueError("result_comparison.mode must be ordered or bag")
        return cls(
            mode="ordered" if mode == "ordered" else "bag",
            absolute_tolerance=_tolerance(
                options.get("absolute_tolerance", 0), "absolute_tolerance"
            ),
            relative_tolerance=_tolerance(
                options.get("relative_tolerance", 0), "relative_tolerance"
            ),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "absolute_tolerance": str(self.absolute_tolerance),
            "relative_tolerance": str(self.relative_tolerance),
        }


def _is_null(value: Any) -> bool:
    return (
        value is None
        or value is pd.NA
        or value is pd.NaT
        or isinstance(value, float)
        and math.isnan(value)
    )


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    parsed = Decimal(str(value))
    return parsed if parsed.is_finite() else None


def values_equal(actual: Any, expected: Any, options: ResultComparison) -> bool:
    if _is_null(actual) or _is_null(expected):
        return _is_null(actual) and _is_null(expected)
    left, right = _number(actual), _number(expected)
    if isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool) and left is None:
        return False
    if (
        isinstance(expected, (int, float, Decimal))
        and not isinstance(expected, bool)
        and right is None
    ):
        return False
    if left is not None or right is not None:
        if left is None or right is None:
            return False
        tolerance = max(options.absolute_tolerance, options.relative_tolerance * abs(right))
        return abs(left - right) <= tolerance
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual == expected
    if isinstance(actual, datetime) or isinstance(expected, datetime):
        return (
            isinstance(actual, datetime) and isinstance(expected, datetime) and actual == expected
        )
    if isinstance(actual, (date, time)) or isinstance(expected, (date, time)):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (bytes, bytearray, memoryview)):
        return isinstance(expected, (bytes, bytearray, memoryview)) and bytes(actual) == bytes(
            expected
        )
    return type(actual) is type(expected) and actual == expected


def compare_rows(
    actual: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    columns: list[str],
    options: ResultComparison,
) -> bool:
    """Compare a sequence or multiset; tolerant bag matching preserves duplicate counts."""
    try:
        columns = validate_column_names(columns)
        actual = read_tabular_result(actual, declared_columns=columns).rows
        expected = read_tabular_result(expected, declared_columns=columns).rows
    except ValueError:
        return False
    if len(actual) != len(expected):
        return False
    if any(set(row) != set(columns) for row in actual + expected):
        return False

    def equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return all(values_equal(left[name], right[name], options) for name in columns)

    if options.mode == "ordered":
        return all(equal(left, right) for left, right in zip(actual, expected, strict=True))
    # A greedy match can incorrectly reject overlapping numeric tolerance ranges.
    adjacency = [
        [index for index, row in enumerate(expected) if equal(value, row)] for value in actual
    ]
    matched: dict[int, int] = {}

    def augment(index: int, visited: set[int]) -> bool:
        for candidate in adjacency[index]:
            if candidate in visited:
                continue
            visited.add(candidate)
            if candidate not in matched or augment(matched[candidate], visited):
                matched[candidate] = index
                return True
        return False

    return all(augment(index, set()) for index in range(len(actual)))
