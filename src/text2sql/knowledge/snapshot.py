"""Immutable, self-contained knowledge inputs consumed by online inference."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RECORD_MODELS
from .provenance import schema_fingerprint
from .schema_contract import SchemaContractError, require_schema_contract
from .structured import (
    KnowledgeBundle,
    KnowledgeValidationError,
    validate_bundle_reviews,
    validate_bundle_schema,
)


@dataclass(frozen=True)
class ArtifactSnapshot:
    schema: dict[str, dict[str, Any]]
    knowledge: KnowledgeBundle
    retrieval_dataset_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "schema_fingerprint": schema_fingerprint(self.schema),
            "knowledge_fingerprint": self.knowledge.fingerprint,
            "schema": self.schema,
            "knowledge": self.knowledge.to_dict(),
            "retrieval_dataset_fingerprint": self.retrieval_dataset_fingerprint,
        }

    @classmethod
    def load(cls, path: str | Path, *, require_reviewed: bool = False) -> ArtifactSnapshot:
        return cls.from_bytes(
            Path(path).read_bytes(), require_reviewed=require_reviewed, source=str(path)
        )

    @classmethod
    def from_bytes(
        cls, content: bytes, *, require_reviewed: bool = False, source: str = "snapshot"
    ) -> ArtifactSnapshot:
        payload = json.loads(content)
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or not isinstance(payload.get("schema"), dict)
        ):
            raise KnowledgeValidationError("invalid knowledge snapshot schema")
        schema = payload["schema"]
        try:
            require_schema_contract(schema)
        except SchemaContractError as exc:
            raise KnowledgeValidationError(str(exc)) from exc
        if not schema or payload.get("schema_fingerprint") != schema_fingerprint(schema):
            raise KnowledgeValidationError("empty or stale schema snapshot")
        records = payload.get("knowledge")
        if not isinstance(records, dict) or set(records) != set(RECORD_MODELS):
            raise KnowledgeValidationError("incomplete knowledge snapshot")
        bundle = KnowledgeBundle(files=[source])
        for name, model in RECORD_MODELS.items():
            if not isinstance(records[name], list):
                raise KnowledgeValidationError(f"invalid snapshot records: {name}")
            setattr(
                bundle,
                name,
                [
                    model.model_validate(item).model_dump(exclude_unset=True)
                    for item in records[name]
                ],
            )
        if payload.get("knowledge_fingerprint") != bundle.fingerprint:
            raise KnowledgeValidationError("knowledge snapshot fingerprint mismatch")
        validate_bundle_schema(bundle, schema)
        if require_reviewed:
            validate_bundle_reviews(bundle, schema)
        if bundle.errors:
            raise KnowledgeValidationError("; ".join(bundle.errors[:12]))
        if payload.get("knowledge_fingerprint") != bundle.fingerprint:
            raise KnowledgeValidationError("snapshot contains unvalidated knowledge records")
        dataset_hash = payload.get("retrieval_dataset_fingerprint")
        if not isinstance(dataset_hash, str) or not dataset_hash:
            raise KnowledgeValidationError("missing retrieval dataset provenance")
        return cls(schema, bundle, dataset_hash)
