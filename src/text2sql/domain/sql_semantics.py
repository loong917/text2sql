"""Verify a bounded aggregate query against its compiled QueryPlan contract."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.scope import Scope

from .query_plan import QueryPlan
from .sql_compiler import SQLCompiler
from .sql_scope import (
    base_tables,
    bind_query,
    canonical_expression,
    name,
    predicate_atoms,
    reachable_scopes,
    table_key,
)


def _predicates(root: Scope) -> set[tuple[Any, ...]]:
    result: set[tuple[Any, ...]] = set()
    for scope in reachable_scopes(root):
        select = scope.expression
        if not isinstance(select, exp.Select):
            raise OptimizeError("当前校验器不支持集合查询分支的业务等价证明")
        if scope is not root and (
            select.args.get("group")
            or select.args.get("limit")
            or select.args.get("distinct")
            or any(select.find_all(exp.AggFunc))
        ):
            raise OptimizeError("派生查询中的预聚合、去重或限行需要单独的语义证明")
        where = select.args.get("where")
        if where is not None:
            atoms = predicate_atoms(where.this, scope)
            if any(item[0] == "metric" for item in atoms):
                raise OptimizeError("聚合阈值必须在 HAVING 中落实，不能置于 WHERE")
            result.update(atoms)
        for join in select.args.get("joins", []):
            if join.args.get("side") or str(join.args.get("kind") or "").upper() not in {
                "",
                "INNER",
            }:
                raise OptimizeError("当前计划只证明 INNER JOIN 的聚合语义")
            on = join.args.get("on")
            if on is not None:
                atoms = predicate_atoms(on, scope)
                if any(item[0] == "metric" for item in atoms):
                    raise OptimizeError("聚合阈值不能置于 JOIN ON")
                result.update(atoms)
        having = select.args.get("having")
        if having is not None:
            atoms = predicate_atoms(having.this, scope)
            if any(item[0] != "metric" for item in atoms):
                raise OptimizeError("实体和日期过滤必须在输入行过滤中落实，不能置于 HAVING")
            result.update(atoms)
    return result


def _groups(root: Scope) -> set[str]:
    group = root.expression.args.get("group")
    return {canonical_expression(item, root) for item in group.expressions} if group else set()


def _validate_query_shape(root: Scope) -> None:
    """Reject row/grain/isolation modifiers outside the compiler's proven subset."""
    allowed_select = {
        "expressions",
        "from_",
        "joins",
        "where",
        "group",
        "having",
        "order",
        "limit",
        "distinct",
        "with_",
    }
    for scope in reachable_scopes(root):
        select = scope.expression
        if not isinstance(select, exp.Select):
            raise OptimizeError("当前计划只证明 SELECT 查询")
        if any(value and key not in allowed_select for key, value in select.args.items()):
            raise OptimizeError("SELECT 包含计划未声明的查询修饰符")
        group = select.args.get("group")
        if group and any(value and key != "expressions" for key, value in group.args.items()):
            raise OptimizeError("ROLLUP/CUBE/GROUPING SETS 未在计划中声明")
        distinct = select.args.get("distinct")
        if distinct and any(distinct.args.values()):
            raise OptimizeError("DISTINCT ON 未在计划中声明")
        for table in select.find_all(exp.Table):
            if any(
                value and key not in {"this", "db", "catalog", "alias"}
                for key, value in table.args.items()
            ):
                raise OptimizeError("表采样、时间版本或表提示无法证明与计划等价")


def _sort(root: Scope) -> list[tuple[str, bool]]:
    order = root.expression.args.get("order")
    return (
        [
            (canonical_expression(item.this, root), bool(item.args.get("desc")))
            for item in order.expressions
        ]
        if order
        else []
    )


def validate_plan_semantics(
    scopes: list[Scope], plan: QueryPlan, schema: dict[str, dict[str, Any]]
) -> str | None:
    if not plan.is_ready:
        return "问题计划需要澄清或尚不支持: " + "；".join(plan.diagnostics or plan.ambiguities)
    compiled = SQLCompiler().compile(plan)
    if compiled is None:
        return "当前语义校验器无法证明该计划的 SQL 等价，请补充关联、粒度或指标定义。"
    if not scopes:
        return "SQL 缺少可验证的查询作用域。"
    actual = scopes[-1]
    if not isinstance(actual.expression, exp.Select):
        return "当前校验器不支持集合查询分支的业务等价证明。"
    try:
        _validate_query_shape(actual)
        expected = bind_query(parse_one(compiled, read="tsql"), schema)[-1]
        actual_tables = Counter(table_key(table) for table in base_tables(actual))
        expected_tables = Counter(table_key(table) for table in base_tables(expected))
        if actual_tables != expected_tables:
            return "SQL 使用的事实表、维度表或重复关联与计划不一致。"
        if any(actual.expression.find_all(exp.Window)):
            return "当前计划不支持窗口函数，无法证明结果粒度。"
        actual_atoms, expected_atoms = _predicates(actual), _predicates(expected)
        missing_atoms = expected_atoms - actual_atoms
        missing_joins = [item for item in missing_atoms if item[0] == "join"]
        if missing_joins:
            return "SQL 缺少计划要求的关联条件。"
        if any(item[0] == "bound" for item in missing_atoms):
            return f"SQL 日期范围必须为 [{plan.date_start}, {plan.date_end})，使用 >= 起始且 < 结束时间。"
        missing_entities = [item for item in missing_atoms if item[0] == "entity"]
        if missing_entities:
            original_columns = {
                (name(intent.table), name(intent.column)): f"{intent.table}.{intent.column}"
                for intent in plan.entity_filter_intents
            }
            columns = "、".join(
                original_columns.get(item[2], f"{item[2][0]}.{item[2][1]}")
                for item in missing_entities
            )
            entities = "、".join(intent.entity for intent in plan.entity_filter_intents)
            return f"SQL 查询分支缺少或错误落实 {columns} 过滤条件，实体 {entities} 与计划不一致。"
        if actual_atoms != expected_atoms:
            return "SQL 过滤或 HAVING 包含计划未声明的条件，无法证明业务等价。"
        if _groups(actual) != _groups(expected):
            return "SQL GROUP BY 与计划的完整分组粒度不一致，不允许遗漏或额外分组字段。"
        actual_projections = Counter(
            canonical_expression(item, actual) for item in actual.expression.expressions
        )
        expected_projections = Counter(
            canonical_expression(item, expected) for item in expected.expression.expressions
        )
        if actual_projections != expected_projections:
            fields = "、".join(metric.column for metric in plan.metrics)
            return f"SQL 投影指标表达式或维度输出与计划不一致，指标字段 {fields} 必须完整绑定。"
        if bool(actual.expression.args.get("distinct")) != bool(plan.distinct):
            return "SQL DISTINCT 与问题要求不一致。"
        limit = actual.expression.args.get("limit")
        if limit is not None and limit.args.get("limit_options"):
            return "TOP PERCENT 或 WITH TIES 未在计划中声明，不能改变返回范围。"
        actual_limit = (
            int(limit.expression.this)
            if limit and isinstance(limit.expression, exp.Literal) and limit.expression.is_int
            else None
        )
        if actual_limit != plan.limit:
            return f"问题要求返回前 {plan.limit} 条，SQL 必须使用对应 TOP/LIMIT。"
        if plan.sort_direction and _sort(actual) != _sort(expected):
            return "SQL 排序指标或方向与计划不一致。"
        if actual.expression.args.get("offset"):
            return "当前计划没有声明 OFFSET，不允许改变结果范围。"
    except OptimizeError as exc:
        return f"SQL 语义无法证明: {exc}"
    return None
