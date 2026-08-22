"""Versioned, strongly typed records for machine-readable business knowledge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictKnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeManifest(StrictKnowledgeModel):
    schema_version: Literal[1]
    name: str = "text2sql-business-knowledge"


class TableCardRecord(StrictKnowledgeModel):
    table: str
    description: str
    business_aliases: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    important_columns: list[str] = Field(default_factory=list)


class MetricRecord(StrictKnowledgeModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    source_table: str
    aggregation: str
    column: str
    unit: str = ""
    output_alias: str = "Value"


class DimensionRecord(StrictKnowledgeModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    table: str
    columns: list[str]
    granularity: str = "grouped"
    kind: str = "dimension"


class JoinRecord(StrictKnowledgeModel):
    id: str
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    relationship: str = ""
    purpose: str = ""


class EntityRecord(StrictKnowledgeModel):
    id: str
    name: str
    table: str
    column: str
    values: dict[str, str]


class PolicyRecord(BaseModel):
    """Policies are extensible, but their identity and kind are mandatory."""

    model_config = ConfigDict(extra="allow")
    id: str
    kind: str


class GoldSqlRecord(StrictKnowledgeModel):
    id: str
    question: str
    sql: str
    tables: list[str]
    semantic_ir: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: Literal["approved", "candidate", "rejected"]


class NegativeSqlRecord(StrictKnowledgeModel):
    id: str
    question: str
    sql: str
    error_types: list[str] = Field(default_factory=list)
    status: Literal["approved", "candidate", "rejected"]


class RefusalRecord(StrictKnowledgeModel):
    id: str
    question: str
    reason: str
    status: Literal["approved", "candidate", "rejected"]


RECORD_MODELS: dict[str, type[BaseModel]] = {
    "table_cards": TableCardRecord,
    "metrics": MetricRecord,
    "dimensions": DimensionRecord,
    "joins": JoinRecord,
    "policies": PolicyRecord,
    "entities": EntityRecord,
    "gold_sql": GoldSqlRecord,
    "negative_sql": NegativeSqlRecord,
    "refusals": RefusalRecord,
}
