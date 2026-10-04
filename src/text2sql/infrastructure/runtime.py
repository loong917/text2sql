"""Lifecycle owner for Ollama, Chroma, and SQL Server adapters."""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import partial
from typing import Any

import chromadb
import ollama
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError
from chromadb.utils import embedding_functions
from vanna.integrations.chromadb import ChromaAgentMemory as VannaChromaAgentMemory
from vanna.integrations.mssql import MSSQLRunner

from ..core.config import Settings
from ..core.http_policy import ollama_http_options
from ..core.logging import setup_logging
from ..knowledge.artifacts import KnowledgeArtifactRegistry
from ..knowledge.collection_evidence import (
    assert_collection_evidence,
    build_collection_evidence,
    normalize_collection_configuration,
)

logger = setup_logging("text2sql.runtime")


def _chroma_client(path: str):
    # Chroma's shared-system cache requires identical settings for the same
    # directory. Memory writers and evidence readers use one explicit policy.
    return chromadb.PersistentClient(
        path=path, settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False)
    )


class ChromaAgentMemory(VannaChromaAgentMemory):
    """Keep Vanna's async memory API while owning the actual query embedding.

    The pinned vendor implementation loads existing collections without its
    injected embedding function. Validate stored identity first, then explicitly
    pass the owned function so queries cannot instantiate another transport or
    silently use a persisted model unrelated to the release model identity.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._collection_lock = threading.RLock()

    def _get_client(self):
        if self._client is None:
            self._client = _chroma_client(self.persist_directory)
        return self._client

    def _get_collection(self):
        with self._collection_lock:
            if self._collection is not None:
                return self._collection
            embedding = self._get_embedding_function()
            expected = embedding.get_config()
            client = self._get_client()
            before = None
            try:
                stored = client.get_collection(name=self.collection_name, embedding_function=None)
                before = normalize_collection_configuration(
                    stored.configuration_json, stored.metadata, expected_embedding=expected
                )
            except NotFoundError:
                collection = client.create_collection(
                    name=self.collection_name,
                    embedding_function=embedding,
                    metadata={"description": "Tool usage memories for learning"},
                )
            else:
                collection = client.get_collection(
                    name=self.collection_name, embedding_function=embedding
                )
            after = normalize_collection_configuration(
                collection.configuration_json, collection.metadata, expected_embedding=expected
            )
            if before is not None and before != after:
                raise ValueError("knowledge collection configuration changed while opening memory")
            self._collection = collection
            return collection


class RuntimeResources:
    """Own lazy process resources for one explicitly injected configuration."""

    def __init__(self, config: Settings):
        self.config = config
        self._sql_runner: MSSQLRunner | None = None
        self._memory_resources: dict[
            str, tuple[ChromaAgentMemory, embedding_functions.OllamaEmbeddingFunction]
        ] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _embedding_config(self) -> dict[str, Any]:
        return {
            "url": f"{self.config.llm_host.rstrip('/')}/api/embeddings",
            "model_name": self.config.embedding_model,
            "timeout": int(self.config.llm_timeout_seconds),
        }

    def _embedding_function(self):
        embedding = embedding_functions.OllamaEmbeddingFunction(**self._embedding_config())
        options = ollama_http_options(self.config.llm_host)
        if not options["trust_env"]:
            # This pinned SDK accepts no client injection. Replace only the owned
            # transport; its public embedding config and collection identity stay intact.
            embedding._client._client.close()
            embedding._client = ollama.Client(
                host=self.config.llm_host,
                timeout=int(self.config.llm_timeout_seconds),
                **options,
            )
        return embedding

    def create_knowledge_memory(self, *, collection_name: str) -> ChromaAgentMemory:
        """Own each explicitly versioned collection and its embedding transport."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime resources are closed")
            if collection_name in self._memory_resources:
                return self._memory_resources[collection_name][0]
            embedding = self._embedding_function()
            try:
                memory = ChromaAgentMemory(
                    persist_directory=self.config.knowledge_db_dir,
                    collection_name=collection_name,
                    embedding_function=embedding,
                )
            except BaseException:
                embedding._client._client.close()
                raise
            self._memory_resources[collection_name] = (memory, embedding)
            return memory

    def _initialize_sql_runner(self) -> None:
        if self._closed:
            raise RuntimeError("Runtime resources are closed")
        if self._sql_runner is None:
            self._sql_runner = MSSQLRunner(
                odbc_conn_str=self.config.mssql_conn_str,
                pool_pre_ping=True,
                pool_size=self.config.sql_pool_size,
                max_overflow=self.config.sql_max_overflow,
                pool_recycle=self.config.sql_pool_recycle_seconds,
            )

    @property
    def sql_runner(self) -> MSSQLRunner:
        with self._lock:
            self._initialize_sql_runner()
            assert self._sql_runner is not None
            return self._sql_runner

    def status(self) -> dict[str, str]:
        artifact = KnowledgeArtifactRegistry(
            self.config.knowledge_artifact_dir,
            self.config.knowledge_active_pointer_path,
        ).load_active()
        with self._lock:
            memory_type = (
                type(next(iter(self._memory_resources.values()))[0]).__name__
                if self._memory_resources
                else "not_initialized"
            )
            runner_type = type(self._sql_runner).__name__ if self._sql_runner else "not_initialized"
        return {
            "llm_model": self.config.llm_model,
            "knowledge_artifact": artifact.version if artifact else "missing_or_incomplete",
            "knowledge_memory": memory_type,
            "sql_runner": runner_type,
        }

    def probe_ollama(self) -> None:
        digests = self.model_digests()
        required = {
            self.config.llm_model: self.config.llm_model_digest,
            self.config.embedding_model: self.config.embedding_model_digest,
        }
        for model, expected_digest in required.items():
            actual_digest = digests.get(model) or digests.get(f"{model}:latest")
            if not actual_digest:
                raise RuntimeError(f"Ollama model is missing: {model}")
            if expected_digest and actual_digest != expected_digest:
                raise RuntimeError(f"Ollama model digest mismatch: {model}")

    def model_digests(self) -> dict[str, str]:
        client = ollama.Client(
            host=self.config.llm_host,
            timeout=min(self.config.llm_timeout_seconds, 5.0),
            **ollama_http_options(self.config.llm_host),
        )
        try:
            response = client.list()
        finally:
            client._client.close()
        models = getattr(response, "models", None)
        if models is None and isinstance(response, dict):
            models = response.get("models", [])
        result: dict[str, str] = {}
        for item in models or []:
            name = getattr(item, "model", None) or getattr(item, "name", None)
            digest = getattr(item, "digest", None)
            if isinstance(item, dict):
                name = name or item.get("model") or item.get("name")
                digest = digest or item.get("digest")
            if name and digest:
                result[str(name)] = str(digest)
        return result

    def collection_evidence(
        self, collection_name: str, *, index_records: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Read records plus fresh storage settings without creating or embedding."""
        client = _chroma_client(self.config.knowledge_db_dir)
        try:
            collection = client.get_collection(collection_name, embedding_function=None)
            before = normalize_collection_configuration(
                collection.configuration_json,
                collection.metadata,
                expected_embedding=self._embedding_config(),
            )
            payload = collection.get(include=["documents", "metadatas", "embeddings"])
            evidence = build_collection_evidence(
                dict(payload),
                configuration=before["configuration"],
                collection_metadata=before["metadata"],
                index_records=index_records,
            )
            # Collection properties are a cached client model. Refetch from storage,
            # rather than comparing that same stale object's properties a second time.
            current = client.get_collection(collection_name, embedding_function=None)
            after = normalize_collection_configuration(
                current.configuration_json,
                current.metadata,
                expected_embedding=self._embedding_config(),
            )
            if before != after or current.count() != evidence["count"]:
                raise ValueError("knowledge collection changed while reading evidence")
            return evidence
        finally:
            client.close()

    def probe_knowledge_collection(
        self,
        collection_name: str,
        expected: dict[str, Any] | None = None,
        *,
        index_records: list[dict[str, Any]] | None = None,
    ) -> None:
        evidence = self.collection_evidence(collection_name, index_records=index_records)
        if expected is not None:
            assert_collection_evidence(evidence, expected)

    def delete_knowledge_collection(self, collection_name: str) -> None:
        """Delete a retired version through Chroma's API, never raw files."""
        client = _chroma_client(self.config.knowledge_db_dir)
        try:
            client.delete_collection(collection_name)
        except Exception as exc:
            # A version can fail before its collection is created. Missing is
            # therefore idempotent, while all other failures must stop pruning.
            if "does not exist" not in str(exc).lower() and "not found" not in str(exc).lower():
                raise
        finally:
            client.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            runner = self._sql_runner
            memories = tuple(self._memory_resources.values())
            self._sql_runner = None
            self._memory_resources.clear()
        failures: list[Exception] = []
        cleanups: list[tuple[str, Callable[[], None]]] = []
        if runner is not None and getattr(runner, "engine", None) is not None:
            cleanups.append(("SQL connection pool", runner.engine.dispose))
        for memory, embedding in memories:
            # Vanna has no close API. Its lazy client and the explicit embedding
            # transport are both owned here; Chroma reference-counts shared systems.
            cleanups.extend(
                (
                    (
                        "knowledge workers",
                        partial(memory._executor.shutdown, wait=False, cancel_futures=True),
                    ),
                    ("embedding transport", embedding._client._client.close),
                )
            )
            client = getattr(memory, "_client", None)
            if client is not None:
                cleanups.append(("knowledge client", client.close))
        for name, cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:
                exc.add_note(f"while closing {name}")
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Runtime cleanup failed", failures)
        logger.info("Runtime resources closed")
