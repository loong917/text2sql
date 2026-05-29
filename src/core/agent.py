from typing import Optional

from vanna import Agent, AgentConfig, ToolRegistry
from vanna.core.user.models import User
from vanna.core.user.request_context import RequestContext
from vanna.core.user.resolver import UserResolver
from vanna.integrations.chromadb import ChromaAgentMemory
from vanna.integrations.mssql import MSSQLRunner
from vanna.integrations.ollama import OllamaLlmService
from vanna.tools import RunSqlTool

from .config import settings
from .exceptions import AgentError
from .logging import setup_logging

logger = setup_logging("text2sql.agent")


class DefaultUserResolver(UserResolver):
    async def resolve_user(self, request_context: RequestContext) -> User:
        return User(id="default_user", username="user")


_agent: Optional[Agent] = None

_sql_runner: Optional[MSSQLRunner] = None


def build_agent() -> Agent:
    logger.info("Building agent instance...")
    try:
        llm_service = OllamaLlmService(
            model=settings.llm_model,
            host=settings.llm_host,
        )
        logger.info(
            "LLM service created: %s @ %s", settings.llm_model, settings.llm_host
        )

        agent_memory = ChromaAgentMemory(
            persist_directory=settings.memory_db_dir,
            collection_name=settings.memory_collection,
        )
        logger.info("Agent memory initialized: %s", settings.memory_db_dir)

        sql_runner = MSSQLRunner(odbc_conn_str=settings.mssql_conn_str)
        sql_tool = RunSqlTool(sql_runner=sql_runner)

        tool_registry = ToolRegistry()
        tool_registry.register_local_tool(sql_tool, access_groups=["admin", "user"])

        user_resolver = DefaultUserResolver()

        agent = Agent(
            llm_service=llm_service,
            tool_registry=tool_registry,
            user_resolver=user_resolver,
            agent_memory=agent_memory,
            config=AgentConfig(stream_responses=True),
        )
        logger.info("Agent instance created successfully")
        return agent
    except Exception as e:
        raise AgentError(f"Failed to build agent: {e}") from e


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


def get_sql_runner() -> MSSQLRunner:
    global _sql_runner
    if _sql_runner is None:
        _sql_runner = MSSQLRunner(odbc_conn_str=settings.mssql_conn_str)
    return _sql_runner


def reset_agent() -> None:
    global _agent, _sql_runner
    _agent = None
    _sql_runner = None
    logger.info("Agent instance reset")
