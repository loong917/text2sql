import unittest

from src.application.context_state import ContextRuntimeState
from src.application.schema_repository import get_live_schema
from src.core.exceptions import DatabaseConnectionError


class FailingExecutor:
    async def execute(self, sql: str, *, timeout_seconds: float):
        raise OSError("driver connection failed")


class SchemaRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_failure_has_safe_operational_message(self):
        with self.assertRaisesRegex(DatabaseConnectionError, "ODBC Driver"):
            await get_live_schema(
                300,
                sql_executor=FailingExecutor(),
                state=ContextRuntimeState(),
            )

    async def test_container_state_cache_avoids_duplicate_database_read(self):
        state = ContextRuntimeState(live_schema={"Fact": {}})
        result = await get_live_schema(
            0,
            sql_executor=FailingExecutor(),
            state=state,
        )
        self.assertEqual(result, {"Fact": {}})


if __name__ == "__main__":
    unittest.main()
