"""T-SQL safety policy and scope-aware QueryPlan validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import OptimizeError, ParseError

from .query_plan import QueryPlan
from .result_contract import validate_column_names
from .sql_scope import bind_query, column_lineage, name, table_key
from .sql_semantics import validate_plan_semantics

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
    """Database-independent restrictions checked before a query can execute."""

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


def _direct_star_projection(tree: exp.Expression) -> bool:
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(expression, exp.Star):
                return True
            if isinstance(expression, exp.Column) and expression.name == "*":
                return True
    return False


def _output_column_names(tree: exp.Query) -> list[str] | None:
    """Use SQL Server labels, not generated optimizer aliases for expressions."""
    labels: list[str] = []
    for projection in tree.selects:
        if isinstance(projection, exp.Alias):
            labels.append(projection.alias)
        elif isinstance(projection, exp.Column) and projection.name != "*":
            labels.append(projection.name)
        elif isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and projection.name == "*"
        ):
            return None  # Qualified scope expansion below supplies the labels.
        else:
            labels.append("")  # Unaliased scalar expressions have no driver label.
    return labels


def validate_tsql_ast(
    sql: str,
    live_schema: dict[str, dict[str, Any]],
    semantic_ir: QueryPlan | None = None,
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
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return "仅允许 SELECT/CTE/集合查询。"
    output_names = _output_column_names(tree)
    if output_names is not None:
        try:
            validate_column_names(output_names)
        except ValueError:
            return "SQL 最终输出列名重复（不区分大小写），请为每列使用唯一别名。"
    policy = safety_policy or SqlSafetyPolicy()
    if len(list(tree.find_all(exp.Join))) > policy.max_joins:
        return f"SQL JOIN 数超过安全上限 {policy.max_joins}。"
    if len(list(tree.find_all(exp.Subquery))) > policy.max_subqueries:
        return f"SQL 子查询数超过安全上限 {policy.max_subqueries}。"
    if not policy.allow_select_star and _direct_star_projection(tree):
        return "数据访问策略禁止直接使用 SELECT *。"
    for table in tree.find_all(exp.Table):
        if table.catalog:
            return f"禁止跨数据库访问: {table.sql(dialect='tsql')}"
        if table.db and name(table.db) not in {name(item) for item in policy.allowed_schemas}:
            return f"SQL 使用了未授权 Schema: {table.db}"
    if not policy.allow_cross_join:
        for join in tree.find_all(exp.Join):
            if str(join.args.get("kind") or "").upper() == "CROSS" or (
                not join.args.get("on") and not join.args.get("using")
            ):
                return "数据访问策略禁止笛卡尔积或缺少关联条件的 JOIN。"
    try:
        scopes = bind_query(tree, live_schema)
        if output_names is None:
            try:
                validate_column_names([projection.alias_or_name for projection in tree.selects])
            except ValueError:
                return "SQL 最终输出列名重复（不区分大小写），请为每列使用唯一别名。"
        tables = [
            source
            for scope in scopes
            for _, source in scope.selected_sources.values()
            if isinstance(source, exp.Table)
        ]
        if policy.require_table and not tables:
            return "查询必须访问经过授权的业务表。"
        schema_names = {name(table) for table in live_schema}
        allowed = {name(table) for table in policy.allowed_tables}
        denied = {name(table) for table in policy.denied_tables}
        for table in tables:
            actual = table_key(table)
            if name(table.db or "dbo") not in {name(item) for item in policy.allowed_schemas}:
                return f"SQL 使用了未授权 Schema: {table.db or 'dbo'}"
            if allowed and actual not in allowed:
                return f"数据访问策略未授权表: {table.name}"
            if actual in denied:
                return f"数据访问策略禁止访问表: {table.name}"
            if actual not in schema_names:
                return f"SQL 使用了不存在的表: {table.name}"
        denied_columns = {name(item) for item in policy.denied_columns}
        for scope in scopes:
            for column in scope.columns:
                lineage = column_lineage(column, scope)
                if lineage and (
                    f"{lineage[0]}.{lineage[1]}" in denied_columns
                    or f"*.{lineage[1]}" in denied_columns
                ):
                    return f"数据访问策略禁止访问字段: {lineage[0]}.{lineage[1]}"
            aggregation_only = {name(item) for item in policy.aggregation_only_tables}
            direct_tables = {
                table_key(source)
                for _, source in scope.selected_sources.values()
                if isinstance(source, exp.Table)
            }
            if direct_tables.intersection(aggregation_only) and isinstance(
                scope.expression, exp.Select
            ):
                select = scope.expression
                aggregates = [
                    aggregate
                    for aggregate in select.find_all(exp.AggFunc)
                    if aggregate.find_ancestor(exp.Select) is select
                    and aggregate.find_ancestor(exp.Window) is None
                ]
                if not aggregates:
                    return "数据访问策略要求目标表在每个查询分支中只能用于聚合查询，窗口函数不能替代聚合。"
                groups = select.args.get("group")
                group_expressions = (
                    {item.sql(dialect="tsql", normalize=True) for item in groups.expressions}
                    if groups
                    else set()
                )
                for projection in select.expressions:
                    value = projection.this if isinstance(projection, exp.Alias) else projection
                    if value.find(exp.Window):
                        return "聚合访问策略禁止使用窗口函数输出原始明细。"
                    if (
                        value.find(exp.Column)
                        and not value.find(exp.AggFunc)
                        and value.sql(dialect="tsql", normalize=True) not in group_expressions
                    ):
                        return "聚合访问策略禁止输出未分组的原始字段。"
        if semantic_ir is not None:
            return validate_plan_semantics(scopes, semantic_ir, live_schema)
    except OptimizeError as exc:
        return f"SQL 字段不存在、存在歧义或无法解析作用域: {exc}"
    return None


def limit_tsql_rows(sql: str, max_rows: int) -> str:
    """Apply a strict row bound without silently redefining baseline semantics.

    PERCENT and WITH TIES are not row bounds and must not be converted into
    ordinary TOP values: doing so would change the independently approved
    baseline's meaning. Reject unsupported bounds before database execution.
    """
    if type(max_rows) is not int or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    statements = parse(sql, read="tsql")
    if len(statements) != 1 or not isinstance(
        statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
    ):
        raise ValueError("row limiting requires a single read-only T-SQL query")
    tree = statements[0]
    if any(tree.find(node) is not None for node in FORBIDDEN_NODES):
        raise ValueError("row limiting cannot admit write operations or dangerous SQL")
    for limit in tree.find_all(exp.Limit, exp.Fetch):
        options = limit.args.get("limit_options")
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            raise ValueError("TOP PERCENT and WITH TIES cannot provide a strict row bound")
    existing_limit = tree.args.get("limit")
    if existing_limit is not None:
        expression = (
            existing_limit.args.get("count")
            if isinstance(existing_limit, exp.Fetch)
            else existing_limit.expression
        )
        if (
            not isinstance(expression, exp.Literal)
            or not expression.is_int
            or int(expression.this) < 0
        ):
            raise ValueError("row limiting requires a non-negative literal query limit")
        if int(expression.this) <= max_rows:
            return tree.sql(dialect="tsql")
    return tree.limit(max_rows).sql(dialect="tsql")
