"""Stable provenance helpers for Schema-bound trusted knowledge."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def schema_fingerprint(schema: dict[str, dict[str, Any]]) -> str:
    """Return a deterministic fingerprint for a normalized live Schema."""
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
