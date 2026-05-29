import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import ConfigurationError
from .logging import setup_logging

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

logger = setup_logging("text2sql.config")


def _resolve_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((ROOT_DIR / path).resolve())


@dataclass(frozen=True)
class Settings:
    llm_model: str
    llm_host: str
    memory_db_dir: str
    memory_collection: str
    mssql_conn_str: str
    server_host: str = "0.0.0.0"
    server_port: int = 8080
    log_level: str = "INFO"
    timeout_keep_alive: int = 5

    def __post_init__(self):
        if not self.mssql_conn_str:
            raise ConfigurationError("MSSQL_CONN_STR is empty")


def load_settings() -> Settings:
    try:
        instance = Settings(
            llm_model=os.getenv("LLM_MODEL", "deepseek-r1:7b"),
            llm_host=os.getenv("LLM_HOST", "http://localhost:11434"),
            memory_db_dir=_resolve_path(os.getenv("MEMORY_DB_DIR", "./vanna_memory_db")),
            memory_collection=os.getenv("MEMORY_COLLECTION", "vanna_agent_memory"),
            mssql_conn_str=os.getenv("MSSQL_CONN_STR", ""),
            server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
            server_port=int(os.getenv("SERVER_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            timeout_keep_alive=int(os.getenv("TIMEOUT_KEEP_ALIVE", "5")),
        )
    except ConfigurationError:
        raise
    except Exception as e:
        raise ConfigurationError(f"Failed to load configuration: {e}") from e

    logger.info(
        "Configuration loaded: model=%s, host=%s:%d",
        instance.llm_model,
        instance.server_host,
        instance.server_port,
    )
    return instance


settings = load_settings()
