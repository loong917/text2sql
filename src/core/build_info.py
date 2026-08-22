"""Stable release metadata used by training and frozen-test attestations."""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path
from typing import Any

from .config import ROOT_DIR, Settings


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        return "missing"
    return hashlib.sha256(source.read_bytes()).hexdigest()


def prompt_fingerprint() -> str:
    """Fingerprint every source file that can materially change generation prompts."""
    digest = hashlib.sha256()
    for relative in (
        "src/application/query_prompt.py",
        "src/application/context_rendering.py",
        "src/infrastructure/ollama_generator.py",
    ):
        path = ROOT_DIR / relative
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in ("vanna", "chromadb", "ollama", "sqlglot", "pyodbc", "fastapi"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "missing"
    return versions


def release_identity(config: Settings) -> dict[str, Any]:
    return {
        "app_revision": config.app_revision,
        "llm_model": config.llm_model,
        "llm_model_digest": config.llm_model_digest,
        "embedding_model": config.embedding_model,
        "embedding_model_digest": config.embedding_model_digest,
        "prompt_fingerprint": prompt_fingerprint(),
        "dependency_versions": dependency_versions(),
        "dependency_lock_fingerprints": {
            "uv.lock": file_sha256(ROOT_DIR / "uv.lock"),
            "requirements.lock": file_sha256(ROOT_DIR / "requirements.lock"),
        },
    }
