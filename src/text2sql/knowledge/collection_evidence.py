"""Content identity for the actual Vanna text-memory Chroma collection."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from numbers import Real
from typing import Any
from urllib.parse import urlsplit

from .source_types import STRUCTURE_ONLY_SOURCE_TYPES

COLLECTION_EVIDENCE_VERSION = 2
COLLECTION_EVIDENCE_KEYS = frozenset(
    {"schema_version", "count", "embedding_dimensions", "configuration_sha256", "sha256"}
)


def validate_collection_evidence(payload: Any) -> list[str]:
    """Reject weak or partial collection attestations, including coerced booleans."""
    if not isinstance(payload, dict) or set(payload) != COLLECTION_EVIDENCE_KEYS:
        return ["collection evidence requires the complete current protocol"]
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != COLLECTION_EVIDENCE_VERSION
    ):
        return [
            "collection evidence schema_version must be 2; rebuild and reevaluate old artifacts"
        ]
    count, dimensions = (
        payload["count"],
        payload["embedding_dimensions"],
    )
    if type(count) is not int or count < 0:
        return ["collection evidence count must be a non-negative integer"]
    if type(dimensions) is not int or dimensions < 0 or (count > 0) != (dimensions > 0):
        return ["nonempty collections require a positive embedding dimension"]
    for field in ("configuration_sha256", "sha256"):
        digest = payload[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return [f"collection evidence {field} must be a lowercase SHA-256 value"]
    return []


def assert_collection_evidence(actual: Any, expected: Any) -> None:
    errors = validate_collection_evidence(actual) + validate_collection_evidence(expected)
    if errors:
        raise ValueError("; ".join(errors))
    if actual != expected:
        raise ValueError(
            "knowledge collection content changed or configuration differs from release evidence"
        )


def _json_value(value: Any) -> Any:
    """Copy finite JSON values, never serialize SDK objects or their repr."""
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("collection configuration must contain only finite JSON values")


def normalize_collection_configuration(
    configuration: Any,
    collection_metadata: Any,
    *,
    expected_embedding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind storage settings and require a verifiable pinned Ollama embedding.

    The full JSON configuration and primitive collection metadata are hashed,
    but only digests leave this module. Unknown/legacy/default embedding configs
    fail closed instead of allowing Chroma to instantiate an uncontrolled model.
    """
    if not isinstance(configuration, dict) or set(configuration) != {
        "hnsw",
        "spann",
        "embedding_function",
    }:
        raise ValueError("collection requires the complete pinned Chroma configuration")
    config = _json_value(configuration)
    index_configs = [config[name] for name in ("hnsw", "spann") if config[name] is not None]
    if len(index_configs) != 1 or not isinstance(index_configs[0], dict):
        raise ValueError("collection requires one explicit vector index configuration")
    index = index_configs[0]
    if index.get("space") not in {"cosine", "l2", "ip"}:
        raise ValueError("collection vector index requires an explicit supported distance")
    if type(index.get("ef_search")) is not int or index["ef_search"] <= 0:
        raise ValueError("collection vector index requires a positive integer ef_search")
    embedding = config["embedding_function"]
    if (
        not isinstance(embedding, dict)
        or set(embedding) != {"type", "name", "config"}
        or embedding.get("type") != "known"
        or embedding.get("name") != "ollama"
        or not isinstance(embedding.get("config"), dict)
        or set(embedding["config"]) != {"url", "model_name", "timeout"}
    ):
        raise ValueError("collection embedding configuration must be a verifiable Ollama function")
    parameters = embedding["config"]
    url, model, timeout = parameters["url"], parameters["model_name"], parameters["timeout"]
    if not isinstance(url, str) or url != url.strip():
        raise ValueError(
            "collection embedding URL must be an explicit noncredentialed HTTP endpoint"
        )
    try:
        parsed = urlsplit(url)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError as exc:
        raise ValueError("collection embedding URL is invalid") from exc
    if not valid_url:
        raise ValueError(
            "collection embedding URL must be an explicit noncredentialed HTTP endpoint"
        )
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError("collection embedding model must be an explicit nonempty name")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("collection embedding timeout must be a positive integer")
    if expected_embedding is not None and parameters != expected_embedding:
        raise ValueError(
            "collection embedding configuration differs from the controlled runtime model"
        )
    metadata = None if collection_metadata is None else _primitive_metadata(collection_metadata)
    return {"configuration": config, "metadata": metadata}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _primitive_metadata(value: Any) -> dict[str, str | bool | int | float]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("collection metadata must be an object with string keys")
    result: dict[str, str | bool | int | float] = {}
    for key, item in value.items():
        if type(item) not in {str, bool, int, float} or (
            isinstance(item, float) and not math.isfinite(item)
        ):
            raise ValueError("collection metadata must contain finite primitive values")
        result[key] = item
    return result


def _registered_texts(index_records: list[dict[str, Any]]) -> set[str]:
    if not isinstance(index_records, list):
        raise ValueError("knowledge index must be an array of registered records")
    texts: set[str] = set()
    for record in index_records:
        if not isinstance(record, dict):
            raise ValueError("knowledge index entries must be objects")
        content, source_type = record.get("content"), record.get("source_type")
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(source_type, str)
            or not source_type.strip()
            or source_type != source_type.strip()
        ):
            raise ValueError("knowledge index entries require content and source_type")
        if source_type not in STRUCTURE_ONLY_SOURCE_TYPES:
            texts.add(content)
    return texts


def build_collection_evidence(
    payload: dict[str, Any],
    *,
    configuration: dict[str, Any],
    collection_metadata: dict[str, Any] | None,
    index_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hash storage configuration and sorted records without embedding any text.

    The pinned Vanna adapter stores each text memory as a Chroma document with
    metadata ``content``, ``timestamp`` and ``is_text_memory=True``. The index
    is deduplicated after writes, so multiple IDs for the same registered text
    are valid; neither missing registered text nor additional text is valid.
    """
    if not isinstance(payload, dict):
        raise ValueError("collection get result must be an object")
    configuration_identity = normalize_collection_configuration(configuration, collection_metadata)
    arrays: list[list[Any]] = []
    for field in ("ids", "documents", "metadatas", "embeddings"):
        value: Any = payload.get(field)
        if hasattr(value, "tolist"):
            value = value.tolist()
        if not isinstance(value, list):
            raise ValueError(f"collection get result requires {field}")
        arrays.append(value)
    ids, documents, metadatas, embeddings = arrays
    count = len(ids)
    if any(len(value) != count for value in arrays):
        raise ValueError("collection result arrays must have equal lengths")
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != count:
        raise ValueError("collection record IDs must be unique nonempty strings")
    records: list[dict[str, Any]] = []
    observed_texts: set[str] = set()
    dimensions = 0
    for record_id, document, metadata_value, vector_value in zip(
        ids, documents, metadatas, embeddings, strict=True
    ):
        if not isinstance(document, str) or not document.strip():
            raise ValueError("collection documents must be nonempty strings")
        metadata = _primitive_metadata(metadata_value)
        if metadata.get("is_text_memory") is not True or metadata.get("content") != document:
            raise ValueError("collection must contain only matching Vanna text memories")
        timestamp = metadata.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            raise ValueError("collection text-memory timestamps must be nonempty strings")
        if "T" not in timestamp or timestamp != timestamp.strip():
            raise ValueError("collection text-memory timestamps must include an explicit time")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("collection text-memory timestamp is invalid") from exc
        if hasattr(vector_value, "tolist"):
            vector_value = vector_value.tolist()
        if not isinstance(vector_value, list) or not vector_value:
            raise ValueError("collection embeddings must be nonempty vectors")
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in vector_value):
            raise ValueError("collection embedding components must be numeric")
        try:
            vector = [float(value) for value in vector_value]
        except (OverflowError, ValueError) as exc:
            raise ValueError("collection embedding components must be finite numbers") from exc
        if any(not math.isfinite(value) for value in vector) or not any(vector):
            raise ValueError("collection embeddings must be finite nonzero vectors")
        if dimensions and dimensions != len(vector):
            raise ValueError("collection embeddings must have a consistent dimension")
        dimensions = len(vector)
        observed_texts.add(document)
        records.append(
            {"id": record_id, "document": document, "metadata": metadata, "embedding": vector}
        )
    if index_records is not None and observed_texts != _registered_texts(index_records):
        raise ValueError("knowledge collection text does not match its registered index")
    content = _canonical_bytes(
        {"storage": configuration_identity, "records": sorted(records, key=lambda item: item["id"])}
    )
    return {
        "schema_version": COLLECTION_EVIDENCE_VERSION,
        "count": count,
        "embedding_dimensions": dimensions,
        "configuration_sha256": hashlib.sha256(
            _canonical_bytes(configuration_identity)
        ).hexdigest(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
