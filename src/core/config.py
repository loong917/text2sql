import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional
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


def _validate_llm_model(model: str) -> str:
    normalized = model.strip().lower()
    unsupported_prefixes = ("deepseek-r1",)
    if normalized.startswith(unsupported_prefixes):
        raise ConfigurationError(
            "LLM_MODEL '%s' does not support tool calling in Ollama. "
            "This project requires a tools-capable model such as "
            "'qwen2.5-coder:7b' or 'llama3.1:8b'." % model
        )
    return model


@dataclass(frozen=True)
class Settings:
    llm_model: str
    llm_host: str
    llm_timeout_seconds: float
    # 知识库目录（Schema、文档、样本数据）
    knowledge_db_dir: str
    # 知识索引文件（sidecar metadata）
    knowledge_index_path: str
    training_manifest_path: str
    training_report_path: str
    training_state_path: str
    retrieval_train_set_path: str
    eval_dev_set_path: str
    eval_test_set_path: str
    training_eval_split: str
    feedback_examples_path: str
    feedback_pending_path: str
    feedback_negative_path: str
    feedback_review_path: str
    table_retrieval_calibrator_path: str
    structured_knowledge_dir: str
    # Agent 记忆目录（对话历史、Agent 状态）
    agent_memory_dir: str
    mssql_conn_str: str
    # 知识库Collection名称
    knowledge_collection: str = "knowledge_memory"
    # Agent 记忆Collection名称
    agent_collection: str = "agent_memory"
    server_host: str = "0.0.0.0"
    server_port: int = 8090
    log_level: str = "INFO"
    timeout_keep_alive: int = 5

    sample_tables: Optional[str] = None
    training_tables: Optional[str] = None
    profiling_max_distinct_values: int = 12
    profiling_max_tables: int = 24
    profiling_max_columns_per_table: int = 8
    training_skip_unchanged: bool = True
    enable_feedback_capture: bool = True
    feedback_require_execution_success: bool = True
    feedback_require_nonempty_result: bool = True
    feedback_min_result_rows: int = 1
    feedback_min_quality_score: int = 75

    embedding_model: str = "bge-m3"
    table_retrieval_token_budget: int = 2400
    table_retrieval_require_calibration: bool = True
    table_retrieval_train_schema_source: str = "auto"
    table_retrieval_max_false_positive_rate: float = 0.25

    # 生产加固参数
    # LLM 并发闸门：本地 Ollama 同时能处理的生成请求数
    llm_max_concurrency: int = 2
    llm_num_ctx: int = 8192
    llm_num_predict: int = 1024
    llm_keep_alive: str = "15m"
    # 单次查询返回给前端的最大行数（超出截断）
    max_result_rows: int = 500
    # 实时 schema 缓存有效期（秒），过期后自动重新读取数据库元数据
    schema_cache_ttl_seconds: int = 300
    # 注入 prompt 的已验证反馈样本条数（0 表示关闭）
    prompt_feedback_examples: int = 3
    # 可选 API Key（为空则不启用鉴权），客户端通过 X-API-Key 请求头携带
    api_key: Optional[str] = None

    def __post_init__(self):
        if not self.mssql_conn_str:
            raise ConfigurationError("MSSQL_CONN_STR is empty")
        if self.training_eval_split not in {"dev", "test"}:
            raise ConfigurationError("TRAINING_EVAL_SPLIT must be 'dev' or 'test'")


def load_settings() -> Settings:
    try:
        instance = Settings(
            llm_model=_validate_llm_model(os.getenv("LLM_MODEL", "qwen2.5-coder:14b")),
            llm_host=os.getenv("LLM_HOST", "http://localhost:11434"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
            # 知识库目录
            knowledge_db_dir=_resolve_path(
                os.getenv("KNOWLEDGE_DB_DIR", "./vanna_knowledge_db")
            ),
            knowledge_index_path=_resolve_path(
                os.getenv(
                    "KNOWLEDGE_INDEX_PATH", "./vanna_knowledge_db/knowledge_index.json"
                )
            ),
            training_manifest_path=_resolve_path(
                os.getenv(
                    "TRAINING_MANIFEST_PATH",
                    "./vanna_knowledge_db/training_manifest.json",
                )
            ),
            training_report_path=_resolve_path(
                os.getenv(
                    "TRAINING_REPORT_PATH",
                    "./vanna_knowledge_db/training_report.json",
                )
            ),
            training_state_path=_resolve_path(
                os.getenv(
                    "TRAINING_STATE_PATH",
                    "./vanna_knowledge_db/training_state.json",
                )
            ),
            retrieval_train_set_path=_resolve_path(
                os.getenv(
                    "RETRIEVAL_TRAIN_SET_PATH",
                    "./evaluation/retrieval_train.jsonl",
                )
            ),
            eval_dev_set_path=_resolve_path(
                os.getenv("EVAL_DEV_SET_PATH", "./evaluation/dev.jsonl")
            ),
            eval_test_set_path=_resolve_path(
                os.getenv("EVAL_TEST_SET_PATH", "./evaluation/test.jsonl")
            ),
            training_eval_split=os.getenv("TRAINING_EVAL_SPLIT", "dev").lower(),
            feedback_examples_path=_resolve_path(
                os.getenv(
                    "FEEDBACK_EXAMPLES_PATH",
                    "./vanna_knowledge_db/feedback_examples.jsonl",
                )
            ),
            feedback_pending_path=_resolve_path(
                os.getenv(
                    "FEEDBACK_PENDING_PATH",
                    "./vanna_knowledge_db/feedback_pending.jsonl",
                )
            ),
            feedback_negative_path=_resolve_path(
                os.getenv(
                    "FEEDBACK_NEGATIVE_PATH",
                    "./vanna_knowledge_db/feedback_negative.jsonl",
                )
            ),
            feedback_review_path=_resolve_path(
                os.getenv(
                    "FEEDBACK_REVIEW_PATH",
                    "./vanna_knowledge_db/feedback_reviews.jsonl",
                )
            ),
            table_retrieval_calibrator_path=_resolve_path(
                os.getenv(
                    "TABLE_RETRIEVAL_CALIBRATOR_PATH",
                    "./vanna_knowledge_db/table_retrieval_calibrator.json",
                )
            ),
            structured_knowledge_dir=_resolve_path(
                os.getenv("STRUCTURED_KNOWLEDGE_DIR", "./knowledge")
            ),
            knowledge_collection=os.getenv("KNOWLEDGE_COLLECTION", "knowledge_memory"),
            # Agent 记忆目录
            agent_memory_dir=_resolve_path(
                os.getenv("AGENT_MEMORY_DIR", "./vanna_agent_memory")
            ),
            agent_collection=os.getenv("AGENT_COLLECTION", "agent_memory"),
            mssql_conn_str=os.getenv("MSSQL_CONN_STR", ""),
            server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
            server_port=int(os.getenv("SERVER_PORT", "8090")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            timeout_keep_alive=int(os.getenv("TIMEOUT_KEEP_ALIVE", "5")),
            sample_tables=os.getenv("SAMPLE_TABLES"),
            training_tables=os.getenv("TRAINING_TABLES"),
            profiling_max_distinct_values=int(
                os.getenv("PROFILING_MAX_DISTINCT_VALUES", "12")
            ),
            profiling_max_tables=int(os.getenv("PROFILING_MAX_TABLES", "24")),
            profiling_max_columns_per_table=int(
                os.getenv("PROFILING_MAX_COLUMNS_PER_TABLE", "8")
            ),
            training_skip_unchanged=os.getenv(
                "TRAINING_SKIP_UNCHANGED", "true"
            ).lower()
            not in {"0", "false", "no"},
            enable_feedback_capture=os.getenv("ENABLE_FEEDBACK_CAPTURE", "true").lower()
            not in {"0", "false", "no"},
            feedback_require_execution_success=os.getenv(
                "FEEDBACK_REQUIRE_EXECUTION_SUCCESS", "true"
            ).lower()
            not in {"0", "false", "no"},
            feedback_require_nonempty_result=os.getenv(
                "FEEDBACK_REQUIRE_NONEMPTY_RESULT", "true"
            ).lower()
            not in {"0", "false", "no"},
            feedback_min_result_rows=int(os.getenv("FEEDBACK_MIN_RESULT_ROWS", "1")),
            feedback_min_quality_score=int(
                os.getenv("FEEDBACK_MIN_QUALITY_SCORE", "75")
            ),
            embedding_model=os.getenv("EMBEDDING_MODEL", "bge-m3"),
            table_retrieval_token_budget=max(
                256, int(os.getenv("TABLE_RETRIEVAL_TOKEN_BUDGET", "2400"))
            ),
            table_retrieval_require_calibration=os.getenv(
                "TABLE_RETRIEVAL_REQUIRE_CALIBRATION", "true"
            ).lower()
            not in {"0", "false", "no"},
            table_retrieval_train_schema_source=os.getenv(
                "TABLE_RETRIEVAL_TRAIN_SCHEMA_SOURCE", "auto"
            ).lower(),
            table_retrieval_max_false_positive_rate=min(
                1.0,
                max(
                    0.0,
                    float(
                        os.getenv(
                            "TABLE_RETRIEVAL_MAX_FALSE_POSITIVE_RATE", "0.25"
                        )
                    ),
                ),
            ),
            llm_max_concurrency=max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "2"))),
            llm_num_ctx=max(2048, int(os.getenv("LLM_NUM_CTX", "8192"))),
            llm_num_predict=max(128, int(os.getenv("LLM_NUM_PREDICT", "1024"))),
            llm_keep_alive=os.getenv("LLM_KEEP_ALIVE", "15m"),
            max_result_rows=max(1, int(os.getenv("MAX_RESULT_ROWS", "500"))),
            schema_cache_ttl_seconds=max(
                0, int(os.getenv("SCHEMA_CACHE_TTL_SECONDS", "300"))
            ),
            prompt_feedback_examples=max(
                0, int(os.getenv("PROMPT_FEEDBACK_EXAMPLES", "3"))
            ),
            api_key=os.getenv("API_KEY") or None,
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
