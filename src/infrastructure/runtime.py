"""Create and manage Ollama, Chroma, and SQL Server runtime adapters."""

import ollama
from typing import Optional

from vanna.integrations.chromadb import ChromaAgentMemory
from chromadb.utils import embedding_functions
from vanna.integrations.mssql import MSSQLRunner

from ..core.config import settings
from ..core.logging import setup_logging

logger = setup_logging("text2sql.runtime")

_sql_runner: Optional[MSSQLRunner] = None

_knowledge_memory: Optional[ChromaAgentMemory] = None

_agent_memory: Optional[ChromaAgentMemory] = None


def _check_ollama_connectivity() -> None:
    try:
        client = ollama.Client(
            host=settings.llm_host,
            timeout=settings.llm_timeout_seconds,
        )
        response = client.chat(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "Hello"}],
        )
        preview = response["message"]["content"][:80]
        logger.info(
            "Ollama connectivity check passed, preview=%s",
            preview.encode("ascii", "backslashreplace").decode("ascii"),
        )
    except Exception as exc:
        logger.warning("Ollama connectivity check failed: %s", exc)


def _embedding_function():
    return embedding_functions.OllamaEmbeddingFunction(
        url=f"{settings.llm_host.rstrip('/')}/api/embeddings",
        model_name=settings.embedding_model,
        timeout=int(settings.llm_timeout_seconds),
    )


def initialize_runtime() -> None:
    """Lazily initialize process-wide adapter singletons."""
    global _knowledge_memory, _agent_memory, _sql_runner
    if _knowledge_memory is None and _agent_memory is None and _sql_runner is None:
        _check_ollama_connectivity()

    if _knowledge_memory is None or _agent_memory is None:
        embedding = _embedding_function()
        if _knowledge_memory is None:
            _knowledge_memory = ChromaAgentMemory(
                persist_directory=settings.knowledge_db_dir,
                collection_name=settings.knowledge_collection,
                embedding_function=embedding,
            )
            logger.info("Knowledge memory initialized: %s", settings.knowledge_db_dir)
        if _agent_memory is None:
            _agent_memory = ChromaAgentMemory(
                persist_directory=settings.agent_memory_dir,
                collection_name=settings.agent_collection,
                embedding_function=embedding,
            )
            logger.info("Agent memory initialized: %s", settings.agent_memory_dir)

    if _sql_runner is None:
        _sql_runner = MSSQLRunner(odbc_conn_str=settings.mssql_conn_str)
        logger.info("SQL runner initialized")


def get_runtime_status() -> dict[str, str]:
    """Initialize adapters and return their configured runtime types."""
    initialize_runtime()
    return {
        "llm_model": settings.llm_model,
        "knowledge_memory": type(_knowledge_memory).__name__
        if _knowledge_memory
        else "",
        "agent_memory": type(_agent_memory).__name__ if _agent_memory else "",
        "sql_runner": type(_sql_runner).__name__ if _sql_runner else "",
    }


def reset_runtime() -> None:
    """Release references to runtime adapters so they can be rebuilt."""
    global _knowledge_memory, _agent_memory, _sql_runner
    _sql_runner = None
    _knowledge_memory = None
    _agent_memory = None
    logger.info("Runtime resources reset")


def get_knowledge_memory() -> ChromaAgentMemory:
    """Return the semantic knowledge store used for retrieval."""
    global _knowledge_memory
    if _knowledge_memory is None:
        initialize_runtime()
    return _knowledge_memory


def get_agent_memory() -> ChromaAgentMemory:
    """Return Vanna's operational memory store used during tool execution."""
    global _agent_memory
    if _agent_memory is None:
        initialize_runtime()
    return _agent_memory


def get_sql_runner() -> MSSQLRunner:
    """Return the shared SQL Server runner."""
    global _sql_runner
    if _sql_runner is None:
        initialize_runtime()
    return _sql_runner
