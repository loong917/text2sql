"""T-SQL AST validation based on sqlglot."""

from __future__ import annotations

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
)


def _name(value: str) -> str:
    return value.strip("[]").lower()


def _literal_value(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Literal):
        return str(node.this)
    return None


def _equality_values(tree: exp.Expression, column_name: str) -> set[str]:
    values: set[str] = set()
    for equality in tree.find_all(exp.EQ):
        left, right = equality.this, equality.expression
        if isinstance(left, exp.Column) and _name(left.name) == _name(column_name):
            value = _literal_value(right)
            if value is not None:
                values.add(value)
        elif isinstance(right, exp.Column) and _name(right.name) == _name(column_name):
            value = _literal_value(left)
            if value is not None:
                values.add(value)
    for in_node in tree.find_all(exp.In):
        if isinstance(in_node.this, exp.Column) and _name(in_node.this.name) == _name(column_name):
            values.update(
                value for item in in_node.expressions if (value := _literal_value(item)) is not None
            )
    return values


def _date_bounds(tree: exp.Expression, column_name: str) -> tuple[set[str], set[str]]:
    lower: set[str] = set()
    upper: set[str] = set()
    for node_type, target in ((exp.GTE, lower), (exp.GT, lower), (exp.LT, upper), (exp.LTE, upper)):
        for node in tree.find_all(node_type):
            if isinstance(node.this, exp.Column) and _name(node.this.name) == _name(column_name):
                value = _literal_value(node.expression)
                if value is not None:
                    target.add(value)
    return lower, upper


def _select_branches(tree: exp.Expression) -> list[exp.Select]:
    branches = list(tree.find_all(exp.Select))
    return branches or ([tree] if isinstance(tree, exp.Select) else [])


def _branch_uses_table(branch: exp.Select, table_name: str) -> bool:
    return any(_name(table.name) == _name(table_name) for table in branch.find_all(exp.Table))


def _validate_semantics(tree: exp.Expression, ir: QuestionSemanticIR) -> str | None:
    branches = _select_branches(tree)
    fact_branches = [item for item in branches if _branch_uses_table(item, "Stat_Collection")]
    if not fact_branches and "Stat_Collection" in ir.required_tables:
        return "SQL 缺少语义要求的事实表 Stat_Collection。"

    expected_blood_types = set(ir.entity_filters.get("blood_type", ()))
    if expected_blood_types:
        observed: set[str] = set()
        for branch in fact_branches:
            branch_values = _equality_values(branch, "BCType")
            if not branch_values:
                return "SQL 的某个查询分支缺少 BCType 血液类型过滤条件。"
            observed.update(branch_values)
        if not expected_blood_types.issubset(observed):
            return "SQL 的 BCType 过滤与问题要求不一致。"

    expected_cities = set(ir.entity_filters.get("city", ()))
    if expected_cities:
        observed = _equality_values(tree, "City")
        if not expected_cities.issubset(observed):
            return "SQL 缺少城市过滤或城市值未规范化: " + ", ".join(sorted(expected_cities))

    if ir.date_start and ir.date_end:
        for branch in fact_branches:
            lower, upper = _date_bounds(branch, "BCDate")
            if ir.date_start not in lower or ir.date_end not in upper:
                return f"SQL 日期范围必须为 [{ir.date_start}, {ir.date_end})。"

    function_names = {
        str(func.sql_name()).upper() for func in tree.find_all(exp.Func)
    }
    for metric in ir.metrics:
        if metric.aggregate not in function_names:
            return f"SQL 缺少语义要求的聚合函数 {metric.aggregate}。"
        if metric.column != "*" and not any(
            _name(column.name) == _name(metric.column) for column in tree.find_all(exp.Column)
        ):
            return f"SQL 缺少指标字段 {metric.column}。"

    group_columns = {
        _name(column.name)
        for group in tree.find_all(exp.Group)
        for column in group.find_all(exp.Column)
    }
    if "institution" in ir.dimensions and not {"instid", "orgname"}.issubset(group_columns):
        return "按机构统计时必须按 InstID、OrgName 分组。"
    if "city" in ir.dimensions and "city" not in group_columns:
        return "按城市统计时必须按 City 分组。"
    return None


def validate_tsql_ast(
    sql: str,
    live_schema: dict[str, dict[str, Any]],
    semantic_ir: QuestionSemanticIR | None = None,
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
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)) and not tree.find(exp.Select):
        return "仅允许 SELECT/CTE/集合查询。"

    cte_names = {_name(cte.alias_or_name) for cte in tree.find_all(exp.CTE)}
    tables = [table for table in tree.find_all(exp.Table) if _name(table.name) not in cte_names]
    schema_names = {_name(name): name for name in live_schema}
    aliases: dict[str, str] = {}
    for table in tables:
        normalized = _name(table.name)
        if normalized not in schema_names:
            return f"SQL 使用了不存在的表: {table.name}"
        actual = schema_names[normalized]
        aliases[_name(table.alias_or_name)] = actual
        aliases[normalized] = actual

    for column in tree.find_all(exp.Column):
        if not column.table:
            candidate_tables = list(dict.fromkeys(aliases.values()))
            known_columns = {
                _name(name)
                for table_name in candidate_tables
                for name in live_schema[table_name].get("columns", {})
            }
            select_aliases = {
                _name(item.alias)
                for item in tree.find_all(exp.Alias)
                if item.alias
            }
            if _name(column.name) not in known_columns | select_aliases:
                return f"SQL 使用了不存在或无法解析的字段: {column.name}"
            continue
        table_name = aliases.get(_name(column.table))
        if table_name is None:
            continue  # derived-table/CTE aliases are validated by the parser scope
        columns = {_name(name) for name in live_schema[table_name].get("columns", {})}
        if _name(column.name) not in columns:
            return f"SQL 使用了不存在的字段: {table_name}.{column.name}"

    if semantic_ir:
        return _validate_semantics(tree, semantic_ir)
    return None
