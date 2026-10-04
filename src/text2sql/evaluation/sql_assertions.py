"""T-SQL evaluation assertions over parsed structure rather than SQL substrings."""

from __future__ import annotations

import re
from typing import Any

from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope


def parse_query(sql: str) -> exp.Expression | None:
    try:
        statements = parse(sql, read="tsql")
    except ParseError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    return statements[0]


def _table_name(table: exp.Table) -> str:
    return ".".join(item.lower() for item in (table.db, table.name) if item)


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _canonical(node: exp.Expression, aliases: dict[str, str], *, qualified: bool) -> str:
    def transform(item: exp.Expression) -> exp.Expression:
        if isinstance(item, exp.Paren):
            return item.this
        if isinstance(item, exp.Column):
            table = aliases.get(item.table.lower(), item.table.lower()) if qualified else ""
            if qualified:
                table = table.split(".")[-1]
            return exp.column(item.name.lower(), table=table or None)
        if isinstance(item, exp.National):
            return exp.Literal.string(item.this)
        if isinstance(item, exp.Identifier):
            return exp.to_identifier(item.name.lower())
        return item

    normalized = node.transform(transform)
    if isinstance(normalized, exp.EQ):
        values = sorted(
            (normalized.this.sql(dialect="tsql"), normalized.expression.sql(dialect="tsql"))
        )
        return " = ".join(values)
    return normalized.sql(dialect="tsql", pretty=False)


class SqlAssertions:
    def __init__(self, sql: str):
        self.tree = parse_query(sql) if sql.strip() else None
        self.tables: set[str] = set()
        self.columns: list[tuple[exp.Column, dict[str, str]]] = []
        self.filters: list[tuple[exp.Expression, dict[str, str]]] = []
        self.joins: list[tuple[exp.Expression, dict[str, str]]] = []
        if self.tree is None:
            return
        for scope in traverse_scope(self.tree):
            aliases = {
                name.lower(): _table_name(value)
                for name, value in scope.sources.items()
                if isinstance(value, exp.Table)
            }
            self.tables.update(aliases.values())
            self.columns.extend((column, aliases) for column in scope.columns)
            for key in ("where", "having"):
                clause = scope.expression.args.get(key)
                if clause is not None:
                    self.filters.extend((node, aliases) for node in _conjuncts(clause.this))
            for join in scope.expression.args.get("joins") or []:
                clause = join.args.get("on")
                if clause is not None:
                    self.joins.extend((node, aliases) for node in _conjuncts(clause))

    def table(self, name: str) -> bool:
        expected = name.lower().replace("[", "").replace("]", "")
        return any(
            value == expected or "." not in expected and value.split(".")[-1] == expected
            for value in self.tables
        )

    def column(self, name: str) -> bool:
        expected = name.lower().replace("[", "").replace("]", "")
        for column, aliases in self.columns:
            actual = _canonical(column, aliases, qualified="." in expected)
            if actual.lower() == expected:
                return True
        return False

    def predicate(self, expected: str, *, join: bool = False) -> bool:
        try:
            tree = parse_one(f"SELECT 1 WHERE {expected}", read="tsql")
        except ParseError:
            return False
        expression = tree.args["where"].this
        qualified = any(column.table for column in expression.find_all(exp.Column))
        key = _canonical(expression, {}, qualified=qualified)
        candidates = self.joins if join else self.filters
        return any(
            _canonical(node, aliases, qualified=qualified) == key for node, aliases in candidates
        )

    def forbidden(self, expected: str) -> bool:
        """Match forbidden identifiers/operators, excluding comments and string literals."""
        if self.tree is None:
            return False
        if re.fullmatch(r"[\w\[\].]+", expected):
            name = expected.lower().replace("[", "").replace("]", "")
            return (
                self.column(expected)
                or self.table(expected)
                or any(
                    node.key.lower() == name
                    or isinstance(node, exp.Anonymous)
                    and node.name.lower() == name
                    for node in self.tree.walk()
                )
            )
        try:
            expression = parse_one(f"SELECT 1 WHERE {expected}", read="tsql").args["where"].this
        except ParseError:
            return False
        key = _canonical(expression, {}, qualified=False)
        return any(_canonical(node, {}, qualified=False) == key for node in self.tree.walk())

    def checks(self, case: dict[str, Any]) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for field, label, matcher in (
            ("must_include_tables", "table", self.table),
            ("must_include_columns", "column", self.column),
            ("must_include_filters", "filter", self.predicate),
            ("must_include_joins", "join", lambda value: self.predicate(value, join=True)),
            ("must_not_contain", "not_contains", lambda value: not self.forbidden(value)),
            ("must_not_include_columns", "not_column", lambda value: not self.column(value)),
        ):
            for expected in case.get(field, []) or []:
                checks.append(
                    {
                        "name": f"{label}:{expected}",
                        "passed": matcher(expected),
                        "expected": expected,
                        "actual": "AST structure",
                    }
                )
        if "must_have_group_by" in case:
            actual = self.tree is not None and any(self.tree.find_all(exp.Group))
            checks.append(
                {
                    "name": "group_by",
                    "passed": actual == case["must_have_group_by"],
                    "expected": case["must_have_group_by"],
                    "actual": actual,
                }
            )
        return checks
