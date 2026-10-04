"""Versioned, strongly typed records for machine-readable business knowledge."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from ..domain.semantic_contract import SemanticExpectation
from .governance import ExecutionEvidence, ReviewEvidence, content_digest, sql_digest


class StrictKnowledgeModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_min_length=1)


class ReviewedRecord(StrictKnowledgeModel):
    review: ReviewEvidence | None = None

    @model_validator(mode="before")
    @classmethod
    def bind_reviewed_content(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("review"):
            review = ReviewEvidence.model_validate(value["review"])
            if review.content_sha256 != content_digest(value):
                raise ValueError("review evidence does not match current authored content")
        return value


class KnowledgeManifest(StrictKnowledgeModel):
    schema_version: Literal[1]
    name: str = "text2sql-business-knowledge"


class TableCardRecord(ReviewedRecord):
    table: str
    description: str
    business_aliases: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    important_columns: list[str] = Field(default_factory=list)
    grain: str | None = None


class MetricRecord(ReviewedRecord):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    source_table: str
    aggregation: Literal["COUNT", "SUM", "AVG", "MIN", "MAX"]
    column: str
    unit: str = ""
    output_alias: str = "Value"

    @model_validator(mode="after")
    def valid_star(self) -> Self:
        if self.column == "*" and self.aggregation != "COUNT":
            raise ValueError("only COUNT may aggregate '*'")
        return self


class DimensionRecord(ReviewedRecord):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    table: str
    columns: list[str] = Field(min_length=1)
    output_aliases: list[str] = Field(default_factory=list)
    granularity: str = "grouped"
    kind: Literal["dimension", "date"] = "dimension"

    @model_validator(mode="after")
    def validate_columns(self) -> Self:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("dimension columns must be unique")
        if self.output_aliases and len(self.output_aliases) != len(self.columns):
            raise ValueError("output_aliases must match dimension columns")
        if self.kind == "date" and len(self.columns) != 1:
            raise ValueError("date dimensions require exactly one column")
        return self


class JoinRecord(ReviewedRecord):
    id: str
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    relationship: Literal["many_to_one", "one_to_one"]
    purpose: str = ""


class EntityRecord(ReviewedRecord):
    id: str
    name: str
    table: str
    column: str
    values: dict[str, str]


class EntityFilterPolicy(ReviewedRecord):
    id: str
    kind: Literal["entity_filter"]
    entity: str
    terms: list[str] = Field(min_length=1)
    table: str
    column: str
    operator: Literal["="]
    value: str


class RequiredJoinPolicy(ReviewedRecord):
    id: str
    kind: Literal["required_join"]
    terms: list[str] = Field(min_length=1)
    join_id: str


class RefusalPolicy(ReviewedRecord):
    id: str
    kind: Literal["refusal"]
    rule: str


class PolicyRecord(
    RootModel[
        Annotated[
            EntityFilterPolicy | RequiredJoinPolicy | RefusalPolicy,
            Field(discriminator="kind"),
        ]
    ]
):
    """Only implemented policy kinds are admissible; unknown kinds fail closed."""


class GoldSqlRecord(ReviewedRecord):
    id: str
    question: str
    sql: str
    tables: list[str]
    semantic_ir: SemanticExpectation
    tags: list[str] = Field(default_factory=list)
    status: Literal["approved", "candidate", "rejected"]
    execution: ExecutionEvidence | None = None

    @model_validator(mode="after")
    def approval_evidence(self) -> Self:
        if self.status == "approved":
            if self.review is None or self.execution is None:
                raise ValueError("approved Gold requires independent review and execution evidence")
            if self.execution.baseline_sha256 != sql_digest(self.sql):
                raise ValueError("execution evidence does not match Gold SQL")
            if self.review.schema_fingerprint != self.execution.schema_fingerprint:
                raise ValueError("review and execution Schema evidence must agree")
            if self.semantic_ir.date_is_relative or self.semantic_ir.status != "ready":
                raise ValueError("positive Gold requires grounded, absolute-time semantic truth")
        return self


class NegativeSqlRecord(ReviewedRecord):
    id: str
    question: str
    sql: str
    error_types: list[str] = Field(default_factory=list)
    status: Literal["approved", "candidate", "rejected"]

    @model_validator(mode="after")
    def approval_evidence(self) -> Self:
        if self.status == "approved" and self.review is None:
            raise ValueError("approved negative examples require independent review")
        return self


class RefusalRecord(ReviewedRecord):
    id: str
    question: str
    reason: str
    status: Literal["approved", "candidate", "rejected"]

    @model_validator(mode="after")
    def approval_evidence(self) -> Self:
        if self.status == "approved" and self.review is None:
            raise ValueError("approved refusals require independent review")
        return self


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
