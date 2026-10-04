"""Configuration boundary for AST-level SQL safety validation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SqlValidationConfig:
    allowed_schemas: str
    max_joins: int
    max_subqueries: int
    allowed_tables: str = ""
    denied_tables: str = ""
    denied_columns: str = ""
    aggregation_only_tables: str = ""
    allow_select_star: bool = False
    require_table: bool = True
    allow_cross_join: bool = False
