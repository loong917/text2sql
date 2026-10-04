"""Scope-aware column binding and conservative expression lineage for T-SQL."""

from __future__ import annotations

from typing import Any

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope


def name(value: str) -> str:
    return value.strip("[]").lower()


def table_key(table: exp.Table) -> str:
    schema = name(table.db or "dbo")
    table_name = name(table.name)
    return table_name if schema == "dbo" else f"{schema}.{table_name}"


def _schema_location(key: str, details: dict[str, Any]) -> tuple[str, str]:
    parts = key.split(".")
    if len(parts) == 2:
        schema, table = parts
    elif len(parts) == 1:
        schema, table = str(details.get("schema_name") or "dbo"), key
    else:
        raise OptimizeError(f"Schema 表名必须为 table 或 schema.table: {key}")
    return name(schema), name(table)


def bind_query(tree: exp.Expression, schema: dict[str, dict[str, Any]]) -> list[Scope]:
    columns: dict[str, dict[str, dict[str, str]]] = {}
    for table, details in schema.items():
        schema_name, table_name = _schema_location(table, details)
        tables = columns.setdefault(schema_name, {})
        if table_name in tables:
            raise OptimizeError(f"Schema 快照含有重复表定义: {schema_name}.{table_name}")
        tables[table_name] = {name(column): "UNKNOWN" for column in details.get("columns", {})}
    bound = qualify(
        tree,
        dialect="tsql",
        db="dbo",
        schema=columns,
        identify=False,
        quote_identifiers=False,
        infer_schema=False,
        validate_qualify_columns=True,
    )
    return traverse_scope(bound)


def reachable_scopes(scope: Scope) -> list[Scope]:
    result = [scope]
    for _, source in scope.selected_sources.values():
        if isinstance(source, Scope):
            result.extend(reachable_scopes(source))
    return list(dict.fromkeys(result))


def base_tables(scope: Scope) -> list[exp.Table]:
    """Count physical source occurrences, including repeated references to one CTE.

    A scope is a definition, not a relational occurrence. Deduplicating scopes
    here would hide self-joins that multiply aggregate input rows.
    """
    result: list[exp.Table] = []

    def visit(current: Scope, ancestors: frozenset[Scope]) -> None:
        if current in ancestors:
            raise OptimizeError("递归 CTE 尚无聚合语义证明")
        for _, source in current.selected_sources.values():
            if isinstance(source, exp.Table):
                result.append(source)
            elif isinstance(source, Scope):
                visit(source, ancestors | {current})

    visit(scope, frozenset())
    return result


def _source(scope: Scope, alias: str) -> tuple[Scope, exp.Table | Scope] | None:
    current: Scope | None = scope
    while current is not None:
        source = current.sources.get(name(alias))
        if isinstance(source, (exp.Table, Scope)):
            return current, source
        current = current.parent
    return None


def column_lineage(column: exp.Column, scope: Scope) -> tuple[str, str] | None:
    """Resolve direct/renamed projections to a physical column.

    Computed derived columns deliberately return ``None``. Proving arbitrary
    expression equivalence needs a separate capability; parsing alone is not proof.
    """
    if not column.table:
        return None
    resolved = _source(scope, column.table)
    if resolved is None:
        return None
    _, source = resolved
    if isinstance(source, exp.Table):
        return table_key(source), name(column.name)
    if not isinstance(source.expression, exp.Select):
        return None
    for projection in source.expression.expressions:
        if name(projection.alias_or_name) != name(column.name):
            continue
        value = projection.this if isinstance(projection, exp.Alias) else projection
        while isinstance(value, exp.Paren):
            value = value.this
        return column_lineage(value, source) if isinstance(value, exp.Column) else None
    return None


def canonical_expression(node: exp.Expression, scope: Scope) -> str:
    """Replace each bound column by its physical lineage, retaining expression shape."""
    if isinstance(node, exp.Alias):
        node = node.this
    if isinstance(node, exp.Column) and not node.table and isinstance(scope.expression, exp.Select):
        matches = [
            projection
            for projection in scope.expression.expressions
            if projection.alias and name(projection.alias) == name(node.name)
        ]
        if len(matches) == 1:
            node = matches[0].this

    def normalize(item: exp.Expression) -> exp.Expression:
        if isinstance(item, exp.Column):
            lineage = column_lineage(item, scope)
            if lineage is None:
                raise OptimizeError(f"无法证明派生字段血缘: {item.sql(dialect='tsql')}")
            return exp.column(lineage[1], table=lineage[0])
        if isinstance(item, exp.National):
            return exp.Literal.string(str(item.this))
        if isinstance(item, exp.Paren):
            return item.this.transform(normalize)
        return item

    return node.transform(normalize).sql(dialect="tsql", normalize=True)


def _literal(node: exp.Expression) -> str | None:
    if isinstance(node, (exp.Literal, exp.National)):
        return str(node.this)
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return "-" + str(node.this.this)
    return None


def _membership_literal(node: exp.Expression) -> tuple[str, str] | None:
    """Preserve string/number type so implicit casts cannot fake entity equality."""
    value = _literal(node)
    if value is None:
        return None
    is_string = isinstance(node, exp.National) or (isinstance(node, exp.Literal) and node.is_string)
    return ("string" if is_string else "number", value)


def predicate_atoms(node: exp.Expression, scope: Scope) -> set[tuple[Any, ...]]:
    """Normalize conjunctions and same-column membership disjunctions only."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return predicate_atoms(node.this, scope) | predicate_atoms(node.expression, scope)
    if isinstance(node, exp.Or):
        # Only a pure disjunction of membership predicates is equivalent to IN.
        # Flattening an AND nested inside OR changes Boolean meaning, e.g.
        # (city=A AND city=B) OR city=C must never become city IN (A,B,C).
        if node.find(exp.And) is not None:
            raise OptimizeError("OR 分支包含 AND，无法证明其成员过滤与计划等价")
        atoms = predicate_atoms(node.this, scope) | predicate_atoms(node.expression, scope)
        if atoms and all(item[0] == "entity" and item[1] == "in" for item in atoms):
            columns = {item[2] for item in atoms}
            if len(columns) == 1:
                values = tuple(sorted({value for item in atoms for value in item[3]}))
                return {("entity", "in", next(iter(columns)), values)}
        raise OptimizeError("无法证明 OR 条件落实了所有业务过滤约束")
    if isinstance(node, exp.Not):
        atoms = predicate_atoms(node.this, scope)
        if len(atoms) == 1:
            atom = next(iter(atoms))
            if atom[0] == "entity":
                return {("entity", "not_in" if atom[1] == "in" else "in", atom[2], atom[3])}
        raise OptimizeError("无法证明 NOT 条件与计划等价")
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        column = column_lineage(node.this, scope)
        membership_values = [_membership_literal(item) for item in node.expressions]
        if column and membership_values and all(value is not None for value in membership_values):
            return {("entity", "in", column, tuple(sorted(set(membership_values))))}
    classes: tuple[type[exp.Expression], ...] = (exp.EQ, exp.NEQ, exp.GTE, exp.GT, exp.LTE, exp.LT)
    if isinstance(node, classes):
        left, right = node.this, node.expression
        if (
            isinstance(node, exp.EQ)
            and isinstance(left, exp.Column)
            and isinstance(right, exp.Column)
        ):
            left_column, right_column = column_lineage(left, scope), column_lineage(right, scope)
            if left_column and right_column:
                return {("join", tuple(sorted((left_column, right_column))))}
        operations: dict[type[exp.Expression], str] = {
            exp.EQ: "eq",
            exp.NEQ: "neq",
            exp.GTE: "gte",
            exp.GT: "gt",
            exp.LTE: "lte",
            exp.LT: "lt",
        }
        operation = operations[type(node)]
        if _literal(left) is not None and _literal(right) is None:
            left, right = right, left
            operation = {
                "gte": "lte",
                "gt": "lt",
                "lte": "gte",
                "lt": "gt",
                "eq": "eq",
                "neq": "neq",
            }[operation]
        value = _literal(right)
        if value is not None:
            if isinstance(left, exp.Column):
                column = column_lineage(left, scope)
                if column:
                    if operation in {"eq", "neq"}:
                        member = _membership_literal(right)
                        return {
                            ("entity", "in" if operation == "eq" else "not_in", column, (member,))
                        }
                    return {("bound", operation, column, value)}
            if isinstance(left, exp.AggFunc):
                return {("metric", operation, canonical_expression(left, scope), value)}
    raise OptimizeError(f"当前校验器无法证明过滤表达式与计划等价: {node.sql(dialect='tsql')}")
