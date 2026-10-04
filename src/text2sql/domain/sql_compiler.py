"""Compile only catalog-proven, single-fact aggregation plans into T-SQL."""

from __future__ import annotations

import re

from .query_plan import MetricIntent, QueryPlan


def identifier(value: str) -> str:
    return "[" + value.replace("]", "]]") + "]"


def table_identifier(value: str) -> str:
    parts = value.split(".")
    if not 1 <= len(parts) <= 2 or not all(parts):
        raise ValueError("Only table and schema.table identifiers are supported")
    return ".".join(identifier(part) for part in parts)


def _literal(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def _column(table: str, column: str, aliases: dict[str, str]) -> str:
    return f"{aliases[table]}.{identifier(column)}"


def metric_sql(metric: MetricIntent, aliases: dict[str, str]) -> str:
    argument = "*" if metric.column == "*" else _column(metric.source_table, metric.column, aliases)
    return f"{metric.aggregate}({argument})"


def grouping_sql(plan: QueryPlan, aliases: dict[str, str]) -> list[tuple[str, str]]:
    groups: list[tuple[str, str]] = []
    for dimension in plan.dimensions:
        table = plan.dimension_tables[dimension]
        columns = plan.dimension_columns[dimension]
        output_aliases = plan.dimension_output_aliases.get(dimension, ())
        for index, column in enumerate(columns):
            output_alias = output_aliases[index] if index < len(output_aliases) else column
            groups.append((_column(table, column, aliases), output_alias))
    if plan.expected_granularity == "grouped_by_entity":
        for intent in plan.entity_filter_intents:
            if intent.operator == "in" and len(intent.values) > 1:
                groups.append((_column(intent.table, intent.column, aliases), intent.column))
    if plan.time_granularity and plan.date_table and plan.date_column:
        column = _column(plan.date_table, plan.date_column, aliases)
        if plan.time_granularity == "day":
            groups.append((f"CAST({column} AS DATE)", "Date"))
        else:
            groups.append((f"YEAR({column})", "Year"))
            if plan.time_granularity == "month":
                groups.append((f"MONTH({column})", "Month"))
            elif plan.time_granularity == "quarter":
                groups.append((f"DATEPART(QUARTER, {column})", "Quarter"))
    return list(dict.fromkeys(groups))


class SQLCompiler:
    """A supported plan compiles deterministically; other plans return ``None``.

    Supported joins start at the one fact table and extend through declared
    many-to-one or one-to-one edges. Window, ratio, multi-fact and arbitrary
    expression queries are outside this compiler's proof boundary.
    """

    def compile(self, plan: QueryPlan) -> str | None:
        if not plan.is_ready or not plan.metrics or plan.comparison or plan.distinct:
            return None
        if any(
            metric.aggregate not in {"COUNT", "SUM", "AVG", "MIN", "MAX"} for metric in plan.metrics
        ):
            return None
        if any(metric.column == "*" and metric.aggregate != "COUNT" for metric in plan.metrics):
            return None
        fact_tables = {metric.source_table for metric in plan.metrics}
        if len(fact_tables) != 1 or not all(fact_tables):
            return None
        fact = plan.metrics[0].source_table
        tables = list(dict.fromkeys((fact, *plan.required_tables)))
        try:
            for table in tables:
                table_identifier(table)
        except ValueError:
            return None
        aliases = {table: f"t{index}" for index, table in enumerate(tables)}
        joined = {fact}
        join_clauses: list[str] = []
        pending = list(plan.required_joins)
        while pending:
            progress = False
            for join in list(pending):
                if join.relationship not in {"many_to_one", "one_to_one"}:
                    return None
                if join.left_table in joined and join.right_table not in joined:
                    target = join.right_table
                elif (
                    join.relationship == "one_to_one"
                    and join.right_table in joined
                    and join.left_table not in joined
                ):
                    target = join.left_table
                else:
                    continue
                if target not in aliases:
                    return None
                join_clauses.append(
                    f"JOIN {table_identifier(target)} {aliases[target]} ON "
                    f"{_column(join.left_table, join.left_column, aliases)} = "
                    f"{_column(join.right_table, join.right_column, aliases)}"
                )
                joined.add(target)
                pending.remove(join)
                progress = True
            if not progress:
                return None
        if joined != set(tables):
            return None
        try:
            groups = grouping_sql(plan, aliases)
            metrics = {metric.name: metric_sql(metric, aliases) for metric in plan.metrics}
            projections = [f"{expression} AS {identifier(alias)}" for expression, alias in groups]
            projections += [
                f"{metrics[metric.name]} AS {identifier(metric.output_alias)}"
                for metric in plan.metrics
            ]
            filters: list[str] = []
            for intent in plan.entity_filter_intents:
                if not intent.values:
                    return None
                column = _column(intent.table, intent.column, aliases)
                values = ", ".join(_literal(value) for value in intent.values)
                if len(intent.values) == 1:
                    operator = "<>" if intent.operator == "not_in" else "="
                    filters.append(f"{column} {operator} {values}")
                else:
                    operator = "NOT IN" if intent.operator == "not_in" else "IN"
                    filters.append(f"{column} {operator} ({values})")
            if plan.date_start and plan.date_end:
                if not plan.date_table or not plan.date_column:
                    return None
                column = _column(plan.date_table, plan.date_column, aliases)
                filters += [
                    f"{column} >= {_literal(plan.date_start)}",
                    f"{column} < {_literal(plan.date_end)}",
                ]
            modifiers = ""
            if plan.limit is not None:
                if plan.limit < 1 or not plan.sort_direction or not plan.sort_metric:
                    return None
                modifiers += f"TOP ({plan.limit}) "
            sql = f"SELECT {modifiers}{', '.join(projections)} FROM {table_identifier(fact)} {aliases[fact]}"
            if join_clauses:
                sql += " " + " ".join(join_clauses)
            if filters:
                sql += " WHERE " + " AND ".join(filters)
            if groups:
                sql += " GROUP BY " + ", ".join(expression for expression, _ in groups)
            if plan.metric_predicates:
                if any(
                    not re.fullmatch(r"-?\d+(?:\.\d+)?", item.value)
                    for item in plan.metric_predicates
                ):
                    return None
                operators = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "neq": "<>"}
                sql += " HAVING " + " AND ".join(
                    f"{metrics[item.metric]} {operators[item.operator]} {item.value}"
                    for item in plan.metric_predicates
                )
            if plan.sort_direction:
                if plan.sort_metric not in metrics:
                    return None
                sql += f" ORDER BY {metrics[plan.sort_metric]} {plan.sort_direction.upper()}"
            return sql
        except (KeyError, ValueError):
            return None
