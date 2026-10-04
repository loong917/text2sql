"""Atomic publication of one JSON file; used by artifact and CLI adapters."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def atomic_json(path: str | Path, payload: Any) -> None:
    atomic_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def atomic_bytes(path: str | Path, content: bytes) -> None:
    """Replace one file completely, cleaning unpublished temporary bytes on failure."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
