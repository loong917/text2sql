"""Lifecycle owner for Ollama, Chroma, and SQL Server adapters."""

from __future__ import annotations

import threading

import chromadb
import ollama
from chromadb.utils import embedding_functions
from vanna.integrations.chromadb import ChromaAgentMemory
from vanna.integrations.mssql import MSSQLRunner

from ..core.config import Settings
from ..core.logging import setup_logging
from ..knowledge.artifacts import KnowledgeArtifactRegistry

logger = setup_logging("text2sql.runtime")


class RuntimeResources:
    """Own lazy process resources for one explicitly injected configuration."""

    def __init__(self, config: Settings):
        self.config = config
        self._sql_runner: MSSQLRunner | None = None
        self._knowledge_memory: ChromaAgentMemory | None = None
        self._agent_memory: ChromaAgentMemory | None = None
        self._lock = threading.RLock()

    def _embedding_function(self):
        return embedding_functions.OllamaEmbeddingFunction(
            url=f"{self.config.llm_host.rstrip('/')}/api/embeddings",
            model_name=self.config.embedding_model,
            timeout=int(self.config.llm_timeout_seconds),
        )

    def create_knowledge_memory(self, *, collection_name: str) -> ChromaAgentMemory:
        return ChromaAgentMemory(
            persist_directory=self.config.knowledge_db_dir,
            collection_name=collection_name,
            embedding_function=self._embedding_function(),
        )

    def _active_collection_name(self) -> str:
        artifact = KnowledgeArtifactRegistry(
            self.config.knowledge_artifact_dir,
            self.config.knowledge_active_pointer_path,
        ).load_active()
        if artifact is None:
            raise RuntimeError(
                "no complete active knowledge artifact; run text2sql-train before serving queries"
            )
        return artifact.collection_name

    def _initialize_sql_runner(self) -> None:
        if self._sql_runner is None:
            self._sql_runner = MSSQLRunner(
                odbc_conn_str=self.config.mssql_conn_str,
                pool_pre_ping=True,
                pool_size=self.config.sql_pool_size,
                max_overflow=self.config.sql_max_overflow,
                pool_recycle=self.config.sql_pool_recycle_seconds,
            )

    def _initialize_knowledge_memory(self) -> None:
        if self._knowledge_memory is None:
            self._knowledge_memory = ChromaAgentMemory(
                persist_directory=self.config.knowledge_db_dir,
                collection_name=self._active_collection_name(),
                embedding_function=self._embedding_function(),
            )

    def _initialize_agent_memory(self) -> None:
        if self._agent_memory is None:
            self._agent_memory = ChromaAgentMemory(
                persist_directory=self.config.agent_memory_dir,
                collection_name=self.config.agent_collection,
                embedding_function=self._embedding_function(),
            )

    @property
    def sql_runner(self) -> MSSQLRunner:
        with self._lock:
            self._initialize_sql_runner()
            assert self._sql_runner is not None
            return self._sql_runner

    @property
    def knowledge_memory(self) -> ChromaAgentMemory:
        with self._lock:
            self._initialize_knowledge_memory()
            assert self._knowledge_memory is not None
            return self._knowledge_memory

    @property
    def agent_memory(self) -> ChromaAgentMemory:
        with self._lock:
            self._initialize_agent_memory()
            assert self._agent_memory is not None
            return self._agent_memory

    def status(self) -> dict[str, str]:
        artifact = KnowledgeArtifactRegistry(
            self.config.knowledge_artifact_dir,
            self.config.knowledge_active_pointer_path,
        ).load_active()
        return {
            "llm_model": self.config.llm_model,
            "knowledge_artifact": artifact.version if artifact else "missing_or_incomplete",
            "knowledge_memory": type(self._knowledge_memory).__name__
            if self._knowledge_memory
            else "not_initialized",
            "agent_memory": type(self._agent_memory).__name__
            if self._agent_memory
            else "not_initialized",
            "sql_runner": type(self._sql_runner).__name__
            if self._sql_runner
            else "not_initialized",
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
        response = ollama.Client(
            host=self.config.llm_host,
            timeout=min(self.config.llm_timeout_seconds, 5.0),
        ).list()
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

    def probe_knowledge_collection(self, collection_name: str) -> None:
        """Fail when the active artifact points at a missing Chroma collection."""
        chromadb.PersistentClient(path=self.config.knowledge_db_dir).get_collection(collection_name)

    def delete_knowledge_collection(self, collection_name: str) -> None:
        """Delete a retired version through Chroma's API, never raw files."""
        client = chromadb.PersistentClient(path=self.config.knowledge_db_dir)
        try:
            client.delete_collection(collection_name)
        except Exception as exc:
            # A version can fail before its collection is created. Missing is
            # therefore idempotent, while all other failures must stop pruning.
            if "does not exist" not in str(exc).lower() and "not found" not in str(exc).lower():
                raise

    def close(self) -> None:
        with self._lock:
            runner = self._sql_runner
            if runner is not None and getattr(runner, "engine", None) is not None:
                runner.engine.dispose()
            self._sql_runner = None
            self._knowledge_memory = None
            self._agent_memory = None
        logger.info("Runtime resources closed")
