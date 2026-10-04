"""Load and validate environment-backed application settings."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import ConfigurationError
from .logging import setup_logging

logger = setup_logging("text2sql.config")


def _resolve_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    data_root = Path(os.getenv("APP_DATA_DIR", str(Path.cwd()))).resolve()
    return str((data_root / path).resolve())


def _validate_llm_model(model: str) -> str:
    if not model.strip():
        raise ConfigurationError("LLM_MODEL is empty")
    return model.strip()


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration shared by application adapters."""

    llm_model: str
    llm_host: str
    llm_timeout_seconds: float
    knowledge_db_dir: str
    knowledge_artifact_dir: str
    knowledge_active_pointer_path: str
    knowledge_artifact_retention_count: int
    knowledge_training_lock_stale_seconds: int
    schema_snapshot_path: str
    training_state_path: str
    retrieval_train_set_path: str
    eval_dev_set_path: str
    eval_test_set_path: str
    training_eval_split: str
    feedback_db_path: str
    table_retrieval_calibrator_path: str
    retrieval_calibration_set_path: str
    retrieval_test_set_path: str
    structured_knowledge_dir: str
    mssql_conn_str: str
    server_host: str = "0.0.0.0"
    server_port: int = 8090
    log_level: str = "INFO"
    timeout_keep_alive: int = 5

    sample_tables: str | None = None
    training_tables: str | None = None
    profiling_max_distinct_values: int = 12
    profiling_max_tables: int = 24
    profiling_max_columns_per_table: int = 8
    profiling_allowed_columns: str = ""
    profiling_denied_columns: str = ""
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

    llm_max_concurrency: int = 2
    llm_num_ctx: int = 8192
    llm_num_predict: int = 1024
    llm_keep_alive: str = "15m"
    max_result_rows: int = 500
    sql_query_timeout_seconds: float = 30.0
    sql_max_concurrency: int = 4
    sql_pool_size: int = 5
    sql_max_overflow: int = 5
    sql_pool_recycle_seconds: int = 1800
    sql_max_joins: int = 8
    sql_max_subqueries: int = 6
    sql_allowed_schemas: str = "dbo"
    sql_allowed_tables: str = ""
    sql_denied_tables: str = ""
    sql_denied_columns: str = ""
    sql_aggregation_only_tables: str = ""
    sql_allow_select_star: bool = False
    sql_require_table: bool = True
    sql_allow_cross_join: bool = False
    schema_cache_ttl_seconds: int = 300
    prompt_feedback_examples: int = 3
    eval_min_pass_rate: float = 0.85
    eval_min_positive_pass_rate: float = 0.85
    eval_min_refusal_pass_rate: float = 0.95
    eval_min_cases: int = 4
    eval_min_positive_cases: int = 3
    eval_min_refusal_cases: int = 1
    eval_min_semantic_ir_pass_rate: float = 1.0
    eval_min_execution_pass_rate: float = 0.85
    eval_min_retrieval_recall: float = 0.9
    api_key: str | None = None
    feedback_admin_api_key: str | None = None
    app_env: str = "development"
    web_session_secret: str | None = None
    web_session_ttl_seconds: int = 28800
    web_session_cookie_secure: bool = False
    app_revision: str = "unknown"
    llm_model_digest: str = ""
    embedding_model_digest: str = ""
    eval_test_report_path: str = ""
    production_readiness_report_path: str = ""
    production_release_manifest_path: str = ""
    production_eval_min_cases: int = 100
    production_eval_min_positive_cases: int = 80
    production_eval_min_refusal_cases: int = 20
    production_retrieval_calibration_min_cases: int = 30
    production_retrieval_test_min_cases: int = 30

    def __post_init__(self):
        if self.app_env not in {"development", "test", "production"}:
            raise ConfigurationError("APP_ENV must be development, test or production")
        if not self.mssql_conn_str:
            raise ConfigurationError("MSSQL_CONN_STR is empty")
        if self.training_eval_split != "dev":
            raise ConfigurationError(
                "TRAINING_EVAL_SPLIT must be 'dev'; run the frozen test split "
                "with text2sql-evaluate --split test"
            )


def load_settings() -> Settings:
    """Build settings from ``.env`` and process environment variables."""
    data_root = Path(os.getenv("APP_DATA_DIR", str(Path.cwd()))).resolve()
    load_dotenv(data_root / ".env", override=False)
    try:
        instance = Settings(
            llm_model=_validate_llm_model(os.getenv("LLM_MODEL", "qwen2.5-coder:14b")),
            llm_host=os.getenv("LLM_HOST", "http://localhost:11434"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
            knowledge_db_dir=_resolve_path(os.getenv("KNOWLEDGE_DB_DIR", "./vanna_knowledge_db")),
            knowledge_artifact_dir=_resolve_path(
                os.getenv("KNOWLEDGE_ARTIFACT_DIR", "./vanna_knowledge_db/artifacts")
            ),
            knowledge_active_pointer_path=_resolve_path(
                os.getenv(
                    "KNOWLEDGE_ACTIVE_POINTER_PATH",
                    "./vanna_knowledge_db/active_artifact.json",
                )
            ),
            knowledge_artifact_retention_count=max(
                1, int(os.getenv("KNOWLEDGE_ARTIFACT_RETENTION_COUNT", "5"))
            ),
            knowledge_training_lock_stale_seconds=max(
                60, int(os.getenv("KNOWLEDGE_TRAINING_LOCK_STALE_SECONDS", "7200"))
            ),
            schema_snapshot_path=_resolve_path(
                os.getenv(
                    "SCHEMA_SNAPSHOT_PATH",
                    "./knowledge/schema/schema_snapshot.json",
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
            feedback_db_path=_resolve_path(
                os.getenv("FEEDBACK_DB_PATH", "./vanna_knowledge_db/feedback.sqlite3")
            ),
            table_retrieval_calibrator_path=_resolve_path(
                os.getenv(
                    "TABLE_RETRIEVAL_CALIBRATOR_PATH",
                    "./vanna_knowledge_db/table_retrieval_calibrator.json",
                )
            ),
            retrieval_calibration_set_path=_resolve_path(
                os.getenv(
                    "RETRIEVAL_CALIBRATION_SET_PATH",
                    "./evaluation/retrieval_calibration.jsonl",
                )
            ),
            retrieval_test_set_path=_resolve_path(
                os.getenv(
                    "RETRIEVAL_TEST_SET_PATH",
                    "./evaluation/retrieval_test.jsonl",
                )
            ),
            structured_knowledge_dir=_resolve_path(
                os.getenv("STRUCTURED_KNOWLEDGE_DIR", "./knowledge")
            ),
            mssql_conn_str=os.getenv("MSSQL_CONN_STR", ""),
            server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
            server_port=int(os.getenv("SERVER_PORT", "8090")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            timeout_keep_alive=int(os.getenv("TIMEOUT_KEEP_ALIVE", "5")),
            sample_tables=os.getenv("SAMPLE_TABLES"),
            training_tables=os.getenv("TRAINING_TABLES"),
            profiling_max_distinct_values=int(os.getenv("PROFILING_MAX_DISTINCT_VALUES", "12")),
            profiling_max_tables=int(os.getenv("PROFILING_MAX_TABLES", "24")),
            profiling_max_columns_per_table=int(os.getenv("PROFILING_MAX_COLUMNS_PER_TABLE", "8")),
            profiling_allowed_columns=os.getenv("PROFILING_ALLOWED_COLUMNS", ""),
            profiling_denied_columns=os.getenv("PROFILING_DENIED_COLUMNS", ""),
            training_skip_unchanged=os.getenv("TRAINING_SKIP_UNCHANGED", "true").lower()
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
            feedback_min_quality_score=int(os.getenv("FEEDBACK_MIN_QUALITY_SCORE", "75")),
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
                    float(os.getenv("TABLE_RETRIEVAL_MAX_FALSE_POSITIVE_RATE", "0.25")),
                ),
            ),
            llm_max_concurrency=max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "2"))),
            llm_num_ctx=max(2048, int(os.getenv("LLM_NUM_CTX", "8192"))),
            llm_num_predict=max(128, int(os.getenv("LLM_NUM_PREDICT", "1024"))),
            llm_keep_alive=os.getenv("LLM_KEEP_ALIVE", "15m"),
            max_result_rows=max(1, int(os.getenv("MAX_RESULT_ROWS", "500"))),
            sql_query_timeout_seconds=max(1.0, float(os.getenv("SQL_QUERY_TIMEOUT_SECONDS", "30"))),
            sql_max_concurrency=max(1, int(os.getenv("SQL_MAX_CONCURRENCY", "4"))),
            sql_pool_size=max(1, int(os.getenv("SQL_POOL_SIZE", "5"))),
            sql_max_overflow=max(0, int(os.getenv("SQL_MAX_OVERFLOW", "5"))),
            sql_pool_recycle_seconds=max(30, int(os.getenv("SQL_POOL_RECYCLE_SECONDS", "1800"))),
            sql_max_joins=max(1, int(os.getenv("SQL_MAX_JOINS", "8"))),
            sql_max_subqueries=max(0, int(os.getenv("SQL_MAX_SUBQUERIES", "6"))),
            sql_allowed_schemas=os.getenv("SQL_ALLOWED_SCHEMAS", "dbo"),
            sql_allowed_tables=os.getenv("SQL_ALLOWED_TABLES", ""),
            sql_denied_tables=os.getenv("SQL_DENIED_TABLES", ""),
            sql_denied_columns=os.getenv("SQL_DENIED_COLUMNS", ""),
            sql_aggregation_only_tables=os.getenv("SQL_AGGREGATION_ONLY_TABLES", ""),
            sql_allow_select_star=os.getenv("SQL_ALLOW_SELECT_STAR", "false").lower()
            in {"1", "true", "yes"},
            sql_require_table=os.getenv("SQL_REQUIRE_TABLE", "true").lower()
            not in {"0", "false", "no"},
            sql_allow_cross_join=os.getenv("SQL_ALLOW_CROSS_JOIN", "false").lower()
            in {"1", "true", "yes"},
            schema_cache_ttl_seconds=max(0, int(os.getenv("SCHEMA_CACHE_TTL_SECONDS", "300"))),
            prompt_feedback_examples=max(0, int(os.getenv("PROMPT_FEEDBACK_EXAMPLES", "3"))),
            eval_min_pass_rate=min(1.0, max(0.0, float(os.getenv("EVAL_MIN_PASS_RATE", "0.85")))),
            eval_min_positive_pass_rate=min(
                1.0,
                max(0.0, float(os.getenv("EVAL_MIN_POSITIVE_PASS_RATE", "0.85"))),
            ),
            eval_min_refusal_pass_rate=min(
                1.0,
                max(0.0, float(os.getenv("EVAL_MIN_REFUSAL_PASS_RATE", "0.95"))),
            ),
            eval_min_cases=max(1, int(os.getenv("EVAL_MIN_CASES", "4"))),
            eval_min_positive_cases=max(1, int(os.getenv("EVAL_MIN_POSITIVE_CASES", "3"))),
            eval_min_refusal_cases=max(1, int(os.getenv("EVAL_MIN_REFUSAL_CASES", "1"))),
            eval_min_semantic_ir_pass_rate=min(
                1.0, max(0.0, float(os.getenv("EVAL_MIN_SEMANTIC_IR_PASS_RATE", "1.0")))
            ),
            eval_min_execution_pass_rate=min(
                1.0, max(0.0, float(os.getenv("EVAL_MIN_EXECUTION_PASS_RATE", "0.85")))
            ),
            eval_min_retrieval_recall=min(
                1.0, max(0.0, float(os.getenv("EVAL_MIN_RETRIEVAL_RECALL", "0.9")))
            ),
            api_key=os.getenv("API_KEY") or None,
            feedback_admin_api_key=os.getenv("FEEDBACK_ADMIN_API_KEY") or None,
            app_env=os.getenv("APP_ENV", "development").strip().lower(),
            web_session_secret=os.getenv("WEB_SESSION_SECRET") or None,
            web_session_ttl_seconds=max(300, int(os.getenv("WEB_SESSION_TTL_SECONDS", "28800"))),
            web_session_cookie_secure=os.getenv("WEB_SESSION_COOKIE_SECURE", "false").lower()
            in {"1", "true", "yes"},
            app_revision=os.getenv("APP_REVISION", "unknown").strip(),
            llm_model_digest=os.getenv("LLM_MODEL_DIGEST", "").strip(),
            embedding_model_digest=os.getenv("EMBEDDING_MODEL_DIGEST", "").strip(),
            eval_test_report_path=_resolve_path(
                os.getenv(
                    "EVAL_TEST_REPORT_PATH",
                    "./vanna_knowledge_db/test-report-latest.json",
                )
            ),
            production_readiness_report_path=_resolve_path(
                os.getenv(
                    "PRODUCTION_READINESS_REPORT_PATH",
                    "./vanna_knowledge_db/production-readiness.json",
                )
            ),
            production_release_manifest_path=_resolve_path(
                os.getenv(
                    "PRODUCTION_RELEASE_MANIFEST_PATH",
                    "./vanna_knowledge_db/production-release.json",
                )
            ),
            production_eval_min_cases=max(1, int(os.getenv("PRODUCTION_EVAL_MIN_CASES", "100"))),
            production_eval_min_positive_cases=max(
                1, int(os.getenv("PRODUCTION_EVAL_MIN_POSITIVE_CASES", "80"))
            ),
            production_eval_min_refusal_cases=max(
                1, int(os.getenv("PRODUCTION_EVAL_MIN_REFUSAL_CASES", "20"))
            ),
            production_retrieval_calibration_min_cases=max(
                1,
                int(os.getenv("PRODUCTION_RETRIEVAL_CALIBRATION_MIN_CASES", "30")),
            ),
            production_retrieval_test_min_cases=max(
                1, int(os.getenv("PRODUCTION_RETRIEVAL_TEST_MIN_CASES", "30"))
            ),
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
