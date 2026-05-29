import asyncio

from vanna import ToolContext, User
from vanna.capabilities.sql_runner.models import RunSqlToolArgs

from ..core.agent import get_agent, get_sql_runner
from ..core.exceptions import TrainingError
from ..core.logging import setup_logging

logger = setup_logging("text2sql.training")

"""表查询"""
SCHEMA_QUERY = """
                    SELECT  T.NAME AS TABLE_NAME,
	                        P.VALUE AS TABLE_DESCRIPTION
                    FROM SYS.TABLES T
                    LEFT JOIN SYS.EXTENDED_PROPERTIES P
                    ON T.OBJECT_ID = P.MAJOR_ID AND P.MINOR_ID = 0
                    WHERE P.NAME = 'MS_DESCRIPTION'
                """

"""字段查询"""
COLUMN_QUERY = """
                    SELECT  TABLE_SCHEMA,
                            TABLE_NAME,
                            COLUMN_NAME,
                            DATA_TYPE,
                            IS_NULLABLE,
                            VALUE AS COLUMN_DESCRIPTION 
                    FROM INFORMATION_SCHEMA.COLUMNS  T LEFT JOIN SYS.EXTENDED_PROPERTIES P
                    ON OBJECT_ID(T.TABLE_NAME) = P.MAJOR_ID AND T.ORDINAL_POSITION = P.MINOR_ID
                    WHERE NAME = 'MS_DESCRIPTION'
                """

"""业务说明"""
BUSINESS_DOCUMENTATION = """
                                1、数据库名称为 Text2SQL。
                                2、请严格使用 SQL Server 语法，例如 GETDATE(), DATEADD(), DATEPART(), TOP 等。
                        """


def train_knowledge() -> None:
    asyncio.run(_train_async())


async def _train_async() -> None:
    logger.info("Starting knowledge training...")
    try:
        agent = get_agent()
        sql_runner = get_sql_runner()
    except Exception as e:
        raise TrainingError(f"Failed to initialize agent: {e}") from e

    ctx = ToolContext(
        user=User(id="trainer", username="admin"),
        conversation_id="training",
        request_id="train",
        agent_memory=agent.agent_memory,
    )

    try:
        args_schema = RunSqlToolArgs(sql=SCHEMA_QUERY)
        df_schema = await sql_runner.run_sql(args_schema, ctx)
        logger.info("Retrieved %d table definitions from database", len(df_schema))
    except Exception as e:
        raise TrainingError(f"Failed to query table definitions: {e}") from e

    success = 0
    for _, row in df_schema.iterrows():
        try:
            line = f"{row['TABLE_NAME']} ({row['TABLE_DESCRIPTION']})"
            await agent.agent_memory.save_text_memory(line, ctx)
            success += 1
        except Exception:
            logger.warning("Failed to store table entry: %s", row["TABLE_NAME"])

    logger.info("Stored %d/%d table definition memories", success, len(df_schema))

    try:
        args_columns = RunSqlToolArgs(sql=COLUMN_QUERY)
        df_columns = await sql_runner.run_sql(args_columns, ctx)
        logger.info("Retrieved %d column definitions from database", len(df_columns))
    except Exception as e:
        raise TrainingError(f"Failed to query column definitions: {e}") from e

    success = 0
    for _, row in df_columns.iterrows():
        try:
            line = f"{row['TABLE_SCHEMA']}.{row['TABLE_NAME']}.{row['COLUMN_NAME']} ({row['DATA_TYPE']}, {row['COLUMN_DESCRIPTION']})"
            await agent.agent_memory.save_text_memory(line, ctx)
            success += 1
        except Exception:
            logger.warning(
                "Failed to store column entry: %s.%s.%s",
                row["TABLE_SCHEMA"],
                row["TABLE_NAME"],
                row["COLUMN_NAME"],
            )

    logger.info("Stored %d/%d column definition memories", success, len(df_columns))

    try:
        await agent.agent_memory.save_text_memory(BUSINESS_DOCUMENTATION.strip(), ctx)
        logger.info("Stored business documentation")
    except Exception as e:
        raise TrainingError(f"Failed to store documentation: {e}") from e

    logger.info("Knowledge training completed successfully")
