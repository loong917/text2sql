"""T-SQL AST validation based on sqlglot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .semantic_ir import QuestionSemanticIR

FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Into,
)


@dataclass(frozen=True)
class SqlSafetyPolicy:
    """Database-independent limits enforced before a query reaches SQL Server."""

    allowed_schemas: tuple[str, ...] = ("dbo",)
    max_joins: int = 8
    max_subqueries: int = 6
    allowed_tables: tuple[str, ...] = ()
    denied_tables: tuple[str, ...] = ()
    denied_columns: tuple[str, ...] = ()
    aggregation_only_tables: tuple[str, ...] = ()
    allow_select_star: bool = False
    require_table: bool = True
    allow_cross_join: bool = False


def _name(value: str) -> str:
    return value.strip("[]").lower()


def _direct_star_projection(tree: exp.Expression) -> bool:
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(expression, exp.Star):
                return True
            if isinstance(expression, exp.Column) and _name(expression.name) == "*":
                return True
    return False


def _column_is_denied(table: str, column: str, policy: SqlSafetyPolicy) -> bool:
    denied = {_name(item) for item in policy.denied_columns}
    qualified = f"{_name(table)}.{_name(column)}"
    return qualified in denied or f"*.{_name(column)}" in denied


def _literal_value(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Literal):
        return str(node.this)
    return None


def _table_aliases(tree: exp.Expression) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        actual = _name(table.name)
        aliases[actual] = actual
        aliases[_name(table.alias_or_name)] = actual
    return aliases


def _column_matches(
    column: exp.Column,
    *,
    table_name: str,
    column_name: str,
    aliases: dict[str, str],
) -> bool:
    if _name(column.name) != _name(column_name):
        return False
    if not column.table:
        return True
    return aliases.get(_name(column.table)) == _name(table_name)


def _equality_values(
    tree: exp.Expression,
    table_name: str,
    column_name: str,
    aliases: dict[str, str],
) -> set[str]:
    values: set[str] = set()
    for equality in tree.find_all(exp.EQ):
        left, right = equality.this, equality.expression
        if isinstance(left, exp.Column) and _column_matches(
            left, table_name=table_name, column_name=column_name, aliases=aliases
        ):
            value = _literal_value(right)
            if value is not None:
                values.add(value)
        elif isinstance(right, exp.Column) and _column_matches(
            right, table_name=table_name, column_name=column_name, aliases=aliases
        ):
            value = _literal_value(left)
            if value is not None:
                values.add(value)
    for in_node in tree.find_all(exp.In):
        if isinstance(in_node.this, exp.Column) and _column_matches(
            in_node.this,
            table_name=table_name,
            column_name=column_name,
            aliases=aliases,
        ):
            values.update(
                value for item in in_node.expressions if (value := _literal_value(item)) is not None
            )
    return values


def _date_bounds(
    tree: exp.Expression,
    table_name: str,
    column_name: str,
    aliases: dict[str, str],
) -> tuple[set[str], set[str], set[str], set[str]]:
    lower: set[str] = set()
    upper: set[str] = set()
    wrong_lower: set[str] = set()
    wrong_upper: set[str] = set()
    for node_type, target in (
        (exp.GTE, lower),
        (exp.LT, upper),
        (exp.GT, wrong_lower),
        (exp.LTE, wrong_upper),
    ):
        for node in tree.find_all(node_type):
            if isinstance(node.this, exp.Column) and _column_matches(
                node.this,
                table_name=table_name,
                column_name=column_name,
                aliases=aliases,
            ):
                value = _literal_value(node.expression)
                if value is not None:
                    target.add(value)
    return lower, upper, wrong_lower, wrong_upper


def _select_branches(tree: exp.Expression) -> list[exp.Select]:
    branches = list(tree.find_all(exp.Select))
    return branches or ([tree] if isinstance(tree, exp.Select) else [])


def _branch_uses_table(branch: exp.Select, table_name: str) -> bool:
    return any(_name(table.name) == _name(table_name) for table in branch.find_all(exp.Table))


def _validate_semantics(tree: exp.Expression, ir: QuestionSemanticIR) -> str | None:
    branches = _select_branches(tree)
    aliases = _table_aliases(tree)
    table_names = {_name(table.name) for table in tree.find_all(exp.Table)}
    for required_table in ir.required_tables:
        if _name(required_table) not in table_names:
            return f"SQL 缺少语义要求的表 {required_table}。"

    for join in ir.required_joins:
        expected = {
            (_name(join.left_table), _name(join.left_column)),
            (_name(join.right_table), _name(join.right_column)),
        }
        join_found = False
        for node in tree.find_all(exp.EQ):
            if not isinstance(node.this, exp.Column) or not isinstance(node.expression, exp.Column):
                continue
            observed = {
                (aliases.get(_name(column.table), ""), _name(column.name))
                for column in (node.this, node.expression)
            }
            if observed == expected:
                join_found = True
                break
        if not join_found:
            return (
                f"SQL 缺少 {join.left_table}.{join.left_column} = "
                f"{join.right_table}.{join.right_column} 关联条件。"
            )

    for intent in ir.entity_filter_intents:
        fact_branches = [item for item in branches if _branch_uses_table(item, intent.table)]
        observed_values: set[str] = set()
        for branch in fact_branches:
            branch_values = _equality_values(branch, intent.table, intent.column, aliases)
            if not branch_values:
                return f"SQL 的某个查询分支缺少 {intent.column} 过滤条件。"
            observed_values.update(branch_values)
        if not set(intent.values).issubset(observed_values):
            return f"SQL 的 {intent.entity or intent.column} 过滤与问题要求不一致。"

    if ir.date_start and ir.date_end and ir.date_column:
        date_branches = (
            [item for item in branches if _branch_uses_table(item, ir.date_table)]
            if ir.date_table
            else branches
        )
        for branch in date_branches:
            lower, upper, wrong_lower, wrong_upper = _date_bounds(
                branch,
                ir.date_table or "",
                ir.date_column,
                aliases,
            )
            if ir.date_start in wrong_lower or ir.date_end in wrong_upper:
                return "SQL 日期边界必须使用 >= 起始时间且 < 结束时间。"
            if ir.date_start not in lower or ir.date_end not in upper:
                return f"SQL 日期范围必须为 [{ir.date_start}, {ir.date_end})。"

    for metric in ir.metrics:
        matching_aggregates = [
            aggregate
            for aggregate in tree.find_all(exp.AggFunc)
            if str(aggregate.sql_name()).upper() == metric.aggregate
        ]
        if not matching_aggregates:
            return f"SQL 缺少语义要求的聚合函数 {metric.aggregate}。"
        if metric.column == "*":
            if metric.aggregate == "COUNT" and not any(
                any(True for _ in aggregate.find_all(exp.Star)) for aggregate in matching_aggregates
            ):
                return "记录数指标必须使用 COUNT(*)，避免可空字段导致漏计。"
            continue
        metric_bound = any(
            any(
                _column_matches(
                    column,
                    table_name=metric.source_table,
                    column_name=metric.column,
                    aliases=aliases,
                )
                for column in aggregate.find_all(exp.Column)
            )
            for aggregate in matching_aggregates
        )
        if not metric_bound:
            return f"SQL 缺少指标字段 {metric.column}。"

    group_columns = {
        _name(column.name)
        for group in tree.find_all(exp.Group)
        for column in group.find_all(exp.Column)
    }
    for dimension in ir.dimensions:
        required_columns = {_name(item) for item in ir.dimension_columns.get(dimension, ())}
        if required_columns and not required_columns.issubset(group_columns):
            return (
                f"按 {dimension} 统计时必须按 "
                + "、".join(ir.dimension_columns[dimension])
                + " 分组。"
            )
    if ir.distinct and not any(select.args.get("distinct") for select in tree.find_all(exp.Select)):
        return "问题要求结果去重，SQL 必须使用 DISTINCT。"
    if ir.limit is not None:
        limit_node = tree.args.get("limit")
        limit_value = None
        if limit_node is not None and isinstance(limit_node.expression, exp.Literal):
            if limit_node.expression.is_int:
                limit_value = int(limit_node.expression.this)
        if limit_value != ir.limit:
            return f"问题要求返回前 {ir.limit} 条，SQL 必须使用对应 TOP/LIMIT。"
    if ir.sort_direction:
        ordered = list(tree.find_all(exp.Ordered))
        if not ordered:
            return "问题包含排序要求，SQL 必须显式使用 ORDER BY。"
        expected_desc = ir.sort_direction == "desc"
        if not any(bool(item.args.get("desc")) is expected_desc for item in ordered):
            return f"SQL 排序方向必须为 {ir.sort_direction.upper()}。"
    return None


def validate_tsql_ast(
    sql: str,
    live_schema: dict[str, dict[str, Any]],
    semantic_ir: QuestionSemanticIR | None = None,
    safety_policy: SqlSafetyPolicy | None = None,
) -> str | None:
    try:
        statements = parse(sql, read="tsql")
    except ParseError as exc:
        return f"T-SQL AST 解析失败: {exc}"
    if len(statements) != 1:
        return "仅允许一条 SQL 查询。"
    tree = statements[0]
    if tree is None:
        return "SQL 为空。"
    if isinstance(tree, FORBIDDEN_NODES) or any(tree.find(node) for node in FORBIDDEN_NODES):
        return "AST 检测到写操作或危险 SQL，已拒绝执行。"
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)) and not tree.find(
        exp.Select
    ):
        return "仅允许 SELECT/CTE/集合查询。"

    policy = safety_policy or SqlSafetyPolicy()
    if len(list(tree.find_all(exp.Join))) > policy.max_joins:
        return f"SQL JOIN 数超过安全上限 {policy.max_joins}。"
    if len(list(tree.find_all(exp.Subquery))) > policy.max_subqueries:
        return f"SQL 子查询数超过安全上限 {policy.max_subqueries}。"
    if not policy.allow_select_star and _direct_star_projection(tree):
        return "数据访问策略禁止直接使用 SELECT *。"

    cte_names = {_name(cte.alias_or_name) for cte in tree.find_all(exp.CTE)}
    tables = [table for table in tree.find_all(exp.Table) if _name(table.name) not in cte_names]
    if policy.require_table and not tables:
        return "查询必须访问经过授权的业务表。"
    if not policy.allow_cross_join:
        for join in tree.find_all(exp.Join):
            kind = str(join.args.get("kind") or "").upper()
            if kind == "CROSS" or (not join.args.get("on") and not join.args.get("using")):
                return "数据访问策略禁止笛卡尔积或缺少关联条件的 JOIN。"
    schema_names = {_name(name): name for name in live_schema}
    allowed_tables = {_name(item) for item in policy.allowed_tables}
    denied_tables = {_name(item) for item in policy.denied_tables}
    aliases: dict[str, str] = {}
    for table in tables:
        if table.catalog:
            return f"禁止跨数据库访问: {table.sql(dialect='tsql')}"
        if table.db and _name(table.db) not in {_name(item) for item in policy.allowed_schemas}:
            return f"SQL 使用了未授权 Schema: {table.db}"
        normalized = _name(table.name)
        if allowed_tables and normalized not in allowed_tables:
            return f"数据访问策略未授权表: {table.name}"
        if normalized in denied_tables:
            return f"数据访问策略禁止访问表: {table.name}"
        if normalized not in schema_names:
            return f"SQL 使用了不存在的表: {table.name}"
        actual = schema_names[normalized]
        aliases[_name(table.alias_or_name)] = actual
        aliases[normalized] = actual

    for column in tree.find_all(exp.Column):
        if not column.table:
            candidate_tables = list(dict.fromkeys(aliases.values()))
            owners = [
                table_name
                for table_name in candidate_tables
                if _name(column.name)
                in {_name(name) for name in live_schema[table_name].get("columns", {})}
            ]
            select_aliases = {_name(item.alias) for item in tree.find_all(exp.Alias) if item.alias}
            if not owners and _name(column.name) not in select_aliases:
                return f"SQL 使用了不存在或无法解析的字段: {column.name}"
            if len(owners) > 1 and _name(column.name) not in select_aliases:
                return f"SQL 未限定的字段存在歧义: {column.name}"
            if owners and _column_is_denied(owners[0], column.name, policy):
                return f"数据访问策略禁止访问字段: {owners[0]}.{column.name}"
            continue
        table_name = aliases.get(_name(column.table))
        if table_name is None:
            continue  # derived-table/CTE aliases are validated by the parser scope
        columns = {_name(name) for name in live_schema[table_name].get("columns", {})}
        if _name(column.name) not in columns:
            return f"SQL 使用了不存在的字段: {table_name}.{column.name}"
        if _column_is_denied(table_name, column.name, policy):
            return f"数据访问策略禁止访问字段: {table_name}.{column.name}"

    aggregation_only = {_name(item) for item in policy.aggregation_only_tables}
    for select in tree.find_all(exp.Select):
        branch_tables = {
            _name(table.name)
            for table in select.find_all(exp.Table)
            if table.find_ancestor(exp.Select) is select
        }
        if not branch_tables.intersection(aggregation_only):
            continue
        branch_aggregates = [
            aggregate
            for aggregate in select.find_all(exp.AggFunc)
            if aggregate.find_ancestor(exp.Select) is select
        ]
        if not branch_aggregates:
            return "数据访问策略要求目标表在每个查询分支中只能用于聚合查询。"

    if semantic_ir:
        return _validate_semantics(tree, semantic_ir)
    return None


def limit_tsql_rows(sql: str, max_rows: int) -> str:
    """Apply a top-level SQL Server row limit before execution."""
    tree = parse(sql, read="tsql")[0]
    if tree is None:
        return sql
    existing_limit = tree.args.get("limit")
    if existing_limit is not None:
        expression = existing_limit.expression
        if isinstance(expression, exp.Literal) and expression.is_int:
            if int(expression.this) <= max_rows:
                return tree.sql(dialect="tsql")
    return tree.limit(max_rows).sql(dialect="tsql")  # type: ignore[attr-defined]
