"""Dependency-aware readiness checks for the HTTP delivery layer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..retrieval.calibrator import PlattCalibrator
from ..retrieval.dataset import retrieval_dataset_fingerprint


async def build_readiness(config: Settings, container: Any) -> tuple[dict[str, Any], int]:
    """Return granular readiness without hiding which dependency failed."""
    registry = KnowledgeArtifactRegistry(
        config.knowledge_artifact_dir,
        config.knowledge_active_pointer_path,
    )
    active = registry.load_active()
    checks = {
        "knowledge_artifact": "missing_or_incomplete",
        "knowledge_collection": "not_checked",
        "retrieval_calibrator": "not_checked",
        "database": "not_checked",
        "ollama": "not_checked",
    }
    actions: list[str] = []
    if active is None:
        actions.append("run text2sql-train and publish a passing candidate")
        return {"status": "not_ready", "checks": checks, "actions": actions}, 503

    checks["knowledge_artifact"] = active.version
    try:
        await asyncio.to_thread(
            container.runtime.probe_knowledge_collection,
            active.collection_name,
        )
        checks["knowledge_collection"] = "ready"
    except Exception:
        checks["knowledge_collection"] = "missing_or_unreadable"
        actions.append("rebuild and republish the active knowledge artifact")

    if config.table_retrieval_require_calibration:
        try:
            dataset_fingerprint = retrieval_dataset_fingerprint(
                config.retrieval_train_set_path,
                config.retrieval_calibration_set_path,
                config.retrieval_test_set_path,
            )
            calibrator = PlattCalibrator.load(
                Path(active.calibrator_path),
                expected_schema_fingerprint=active.schema_fingerprint,
                expected_embedding_model=config.embedding_model,
                expected_dataset_fingerprint=dataset_fingerprint,
            )
        except Exception:
            calibrator = None
        if calibrator is None:
            checks["retrieval_calibrator"] = "missing_or_stale"
            actions.append("run text2sql-train-retriever against the active schema")
        else:
            checks["retrieval_calibrator"] = "ready"
    else:
        checks["retrieval_calibrator"] = "disabled"

    dependency_results: tuple[Any | BaseException, Any | BaseException] = await asyncio.gather(
        asyncio.to_thread(container.runtime.probe_ollama),
        container.sql_executor.execute("SELECT 1 AS ready", timeout_seconds=5.0),
        return_exceptions=True,
    )
    ollama_result, database_result = dependency_results
    if isinstance(ollama_result, BaseException):
        checks["ollama"] = "failed"
        actions.append("verify LLM_HOST and the configured Ollama models")
    else:
        checks["ollama"] = "ready"
    if isinstance(database_result, BaseException):
        checks["database"] = "failed"
        actions.append("verify MSSQL_CONN_STR, TLS and read-only database access")
    else:
        checks["database"] = "ready"

    ready_values = {"ready", "disabled"}
    is_ready = all(
        value in ready_values or (name == "knowledge_artifact" and value == active.version)
        for name, value in checks.items()
    )
    payload: dict[str, Any] = {
        "status": "ready" if is_ready else "not_ready",
        "checks": checks,
        "actions": actions,
    }
    if is_ready:
        payload["runtime"] = container.runtime.status()
    return payload, 200 if is_ready else 503
