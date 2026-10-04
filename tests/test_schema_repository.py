import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pandas as pd

from text2sql.application.context_state import ContextRuntimeState
from text2sql.core.exceptions import DatabaseConnectionError
from text2sql.infrastructure.schema_repository import (
    LIVE_FK_QUERY,
    LIVE_KEY_QUERY,
    LIVE_SCHEMA_QUERY,
    LIVE_TABLE_QUERY,
    get_live_schema,
)
from text2sql.training.profiling import _distinct_values, _min_max


class FailingExecutor:
    async def execute(self, sql: str, *, timeout_seconds: float):
        raise OSError("driver connection failed")


class MetadataExecutor:
    def __init__(self, tables, *, columns=None, foreign_keys=None, keys=None):
        self.tables = tables
        self.rows = {
            LIVE_TABLE_QUERY: self.tables,
            LIVE_SCHEMA_QUERY: [
                {
                    "TABLE_NAME": "Fact",
                    "SCHEMA_NAME": "audit",
                    "COLUMN_NAME": "ID",
                    "DATA_TYPE": "int",
                    "IS_NULLABLE": False,
                    "MAX_LENGTH": 4,
                    "NUMERIC_PRECISION": 10,
                    "NUMERIC_SCALE": 0,
                    "COLLATION_NAME": None,
                    "IS_IDENTITY": False,
                    "IS_COMPUTED": False,
                },
                {
                    "TABLE_NAME": "Dimension",
                    "SCHEMA_NAME": "dbo",
                    "COLUMN_NAME": "ID",
                    "DATA_TYPE": "int",
                    "IS_NULLABLE": False,
                    "MAX_LENGTH": 4,
                    "NUMERIC_PRECISION": 10,
                    "NUMERIC_SCALE": 0,
                    "COLLATION_NAME": None,
                    "IS_IDENTITY": True,
                    "IS_COMPUTED": False,
                },
            ],
            LIVE_FK_QUERY: [
                {
                    "TABLE_NAME": "Fact",
                    "SCHEMA_NAME": "audit",
                    "COLUMN_NAME": "ID",
                    "REFERENCED_TABLE": "Dimension",
                    "REFERENCED_SCHEMA": "dbo",
                    "REFERENCED_COLUMN": "ID",
                    "CONSTRAINT_NAME": "FK_dim",
                    "COLUMN_ORDINAL": 1,
                    "IS_DISABLED": False,
                    "IS_NOT_TRUSTED": False,
                },
            ],
            LIVE_KEY_QUERY: [
                {
                    "TABLE_NAME": "Dimension",
                    "SCHEMA_NAME": "dbo",
                    "COLUMN_NAME": "ID",
                    "CONSTRAINT_NAME": "PK_dim",
                    "COLUMN_ORDINAL": 1,
                    "IS_PRIMARY_KEY": True,
                },
            ],
        }
        for query, rows in (
            (LIVE_SCHEMA_QUERY, columns),
            (LIVE_FK_QUERY, foreign_keys),
            (LIVE_KEY_QUERY, keys),
        ):
            if rows is not None:
                self.rows[query] = rows

    async def execute(self, sql, *, timeout_seconds):
        return pd.DataFrame(self.rows[sql])


def metadata_executor():
    return MetadataExecutor(
        [
            {"TABLE_NAME": "Fact", "SCHEMA_NAME": "audit", "OBJECT_ID": 10},
            {"TABLE_NAME": "Dimension", "SCHEMA_NAME": "dbo", "OBJECT_ID": 11},
        ]
    )


class SchemaRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_use_qualified_and_escaped_identifiers(self):
        executor = Mock(execute=AsyncMock(return_value=pd.DataFrame({"value": ["sample"]})))
        self.assertEqual(await _distinct_values(executor, "audit.Fact", "Am]ount", 2), ["sample"])
        query = executor.execute.call_args.args[0]
        self.assertIn("FROM [audit].[Fact]", query)
        self.assertIn("[Am]]ount]", query)
        executor.execute.return_value = pd.DataFrame({"min_value": [1], "max_value": [2]})
        self.assertEqual(await _min_max(executor, "audit.Fact", "Am]ount"), ("1", "2"))
        self.assertIn("FROM [audit].[Fact]", executor.execute.call_args.args[0])

    async def test_metadata_preserves_physical_schema_keys_and_relationships(self):
        schema = await get_live_schema(
            0,
            sql_executor=MetadataExecutor(
                [
                    {"TABLE_NAME": "Fact", "SCHEMA_NAME": "audit", "OBJECT_ID": 10},
                    {"TABLE_NAME": "Dimension", "SCHEMA_NAME": "dbo", "OBJECT_ID": 11},
                ]
            ),
            state=ContextRuntimeState(),
        )
        self.assertEqual(set(schema), {"audit.Fact", "Dimension"})
        self.assertEqual(schema["audit.Fact"]["qualified_name"], "audit.Fact")
        fk = schema["audit.Fact"]["foreign_keys"][0]
        self.assertEqual((fk["referenced_table"], fk["referenced_column"]), ("Dimension", "ID"))
        self.assertEqual(schema["Dimension"]["primary_key"], ["ID"])
        self.assertEqual(schema["Dimension"]["unique_keys"], {"PK_dim": ["ID"]})
        self.assertFalse(fk["is_disabled"])
        self.assertFalse(fk["is_not_trusted"])
        self.assertEqual(schema["Dimension"]["columns"]["ID"]["max_length"], 4)
        self.assertEqual(schema["Dimension"]["columns"]["ID"]["precision"], 10)
        self.assertTrue(schema["Dimension"]["columns"]["ID"]["is_identity"])
        self.assertIsNone(schema["Dimension"]["columns"]["ID"]["collation_name"])

    async def test_duplicate_metadata_objects_fail_closed_without_cache_update(self):
        state = ContextRuntimeState()
        duplicate = {"TABLE_NAME": "Fact", "SCHEMA_NAME": "audit", "OBJECT_ID": 10}
        with self.assertRaises(DatabaseConnectionError):
            await get_live_schema(
                0, sql_executor=MetadataExecutor([duplicate, duplicate]), state=state
            )
        self.assertIsNone(state.live_schema)

    async def test_connection_failure_has_safe_operational_message(self):
        with self.assertRaisesRegex(DatabaseConnectionError, "ODBC Driver"):
            await get_live_schema(
                300,
                sql_executor=FailingExecutor(),
                state=ContextRuntimeState(),
            )

    async def test_container_state_cache_avoids_duplicate_database_read(self):
        state = ContextRuntimeState()
        expected = await get_live_schema(0, sql_executor=metadata_executor(), state=state)
        result = await get_live_schema(
            0,
            sql_executor=FailingExecutor(),
            state=state,
        )
        self.assertIs(result, expected)

    async def test_no_physical_keys_are_explicitly_empty_not_invented(self):
        executor = metadata_executor()
        executor.rows[LIVE_FK_QUERY] = []
        executor.rows[LIVE_KEY_QUERY] = []
        schema = await get_live_schema(0, sql_executor=executor, state=ContextRuntimeState())
        for info in schema.values():
            self.assertEqual(info["primary_key"], [])
            self.assertEqual(info["unique_keys"], {})
            self.assertEqual(info["foreign_keys"], [])

    async def test_disabled_and_untrusted_fk_state_is_preserved(self):
        executor = metadata_executor()
        executor.rows[LIVE_FK_QUERY][0].update(IS_DISABLED=1, IS_NOT_TRUSTED=1)
        schema = await get_live_schema(0, sql_executor=executor, state=ContextRuntimeState())
        fk = schema["audit.Fact"]["foreign_keys"][0]
        self.assertIs(fk["is_disabled"], True)
        self.assertIs(fk["is_not_trusted"], True)

    async def test_nullable_bits_are_not_coerced_from_strings_or_nulls(self):
        for value in ("False", "0", None, float("nan"), 2):
            with self.subTest(value=value):
                executor = metadata_executor()
                executor.rows[LIVE_SCHEMA_QUERY][0]["IS_NULLABLE"] = value
                state = ContextRuntimeState()
                with self.assertRaises(DatabaseConnectionError):
                    await get_live_schema(0, sql_executor=executor, state=state)
                self.assertIsNone(state.live_schema)

    async def test_missing_column_type_identity_or_details_fail_closed(self):
        for field in ("DATA_TYPE", "SCHEMA_NAME", "MAX_LENGTH", "IS_COMPUTED"):
            with self.subTest(field=field):
                executor = metadata_executor()
                del executor.rows[LIVE_SCHEMA_QUERY][0][field]
                state = ContextRuntimeState()
                with self.assertRaises(DatabaseConnectionError):
                    await get_live_schema(0, sql_executor=executor, state=state)
                self.assertIsNone(state.live_schema)

    async def test_columns_do_not_create_missing_tables(self):
        executor = metadata_executor()
        executor.rows[LIVE_TABLE_QUERY] = executor.tables[1:]
        state = ContextRuntimeState()
        with self.assertRaises(DatabaseConnectionError):
            await get_live_schema(0, sql_executor=executor, state=state)
        self.assertIsNone(state.live_schema)

    async def test_duplicate_column_unknown_fk_and_key_fail_without_cache_update(self):
        for query, field, value in (
            (LIVE_SCHEMA_QUERY, "COLUMN_NAME", "ID"),
            (LIVE_FK_QUERY, "REFERENCED_COLUMN", "missing"),
            (LIVE_FK_QUERY, "IS_NOT_TRUSTED", None),
            (LIVE_KEY_QUERY, "COLUMN_NAME", "missing"),
            (LIVE_KEY_QUERY, "COLUMN_ORDINAL", 2),
        ):
            with self.subTest(query=query, field=field):
                executor = metadata_executor()
                if query == LIVE_SCHEMA_QUERY:
                    executor.rows[query].append(deepcopy(executor.rows[query][0]))
                else:
                    executor.rows[query][0][field] = value
                state = ContextRuntimeState()
                with self.assertRaises(DatabaseConnectionError):
                    await get_live_schema(0, sql_executor=executor, state=state)
                self.assertIsNone(state.live_schema)

    async def test_failed_force_refresh_preserves_last_valid_cache(self):
        state = ContextRuntimeState()
        previous = await get_live_schema(0, sql_executor=metadata_executor(), state=state)
        cached_at = state.live_schema_cached_at
        executor = metadata_executor()
        executor.rows[LIVE_SCHEMA_QUERY][0]["DATA_TYPE"] = ""
        with self.assertRaises(DatabaseConnectionError):
            await get_live_schema(0, force_refresh=True, sql_executor=executor, state=state)
        self.assertIs(state.live_schema, previous)
        self.assertEqual(state.live_schema_cached_at, cached_at)

    async def test_key_ordinals_are_sorted_and_composite_keys_remain_atomic(self):
        executor = metadata_executor()
        executor.rows[LIVE_SCHEMA_QUERY].append(
            {**executor.rows[LIVE_SCHEMA_QUERY][1], "COLUMN_NAME": "Code"}
        )
        executor.rows[LIVE_KEY_QUERY] = [
            {**executor.rows[LIVE_KEY_QUERY][0], "COLUMN_NAME": "Code", "COLUMN_ORDINAL": 2},
            executor.rows[LIVE_KEY_QUERY][0],
        ]
        executor.rows[LIVE_FK_QUERY] = []
        schema = await get_live_schema(0, sql_executor=executor, state=ContextRuntimeState())
        self.assertEqual(schema["Dimension"]["primary_key"], ["ID", "Code"])
        self.assertEqual(schema["Dimension"]["unique_keys"], {"PK_dim": ["ID", "Code"]})

    async def test_native_sql_bit_values_normalize_to_exact_booleans(self):
        executor = metadata_executor()
        executor.rows[LIVE_SCHEMA_QUERY][0].update(IS_NULLABLE=0, IS_IDENTITY=0, IS_COMPUTED=0)
        executor.rows[LIVE_SCHEMA_QUERY][1].update(IS_NULLABLE=0, IS_IDENTITY=1, IS_COMPUTED=0)
        executor.rows[LIVE_KEY_QUERY][0]["IS_PRIMARY_KEY"] = 1
        schema = await get_live_schema(0, sql_executor=executor, state=ContextRuntimeState())
        self.assertIs(schema["Dimension"]["columns"]["ID"]["is_identity"], True)

    async def test_length_precision_scale_and_collation_are_captured_exactly(self):
        executor = metadata_executor()
        executor.rows[LIVE_SCHEMA_QUERY][0].update(
            DATA_TYPE="nvarchar",
            MAX_LENGTH=-1,
            NUMERIC_PRECISION=0,
            NUMERIC_SCALE=0,
            COLLATION_NAME="Chinese_PRC_CI_AS",
            IS_COMPUTED=True,
        )
        schema = await get_live_schema(0, sql_executor=executor, state=ContextRuntimeState())
        column = schema["audit.Fact"]["columns"]["ID"]
        self.assertEqual(column["max_length"], -1)
        self.assertEqual(column["collation_name"], "Chinese_PRC_CI_AS")
        self.assertIs(column["is_computed"], True)

    async def test_collation_values_are_not_silently_replaced_with_null(self):
        for value in (123, False, ""):
            with self.subTest(value=value):
                executor = metadata_executor()
                executor.rows[LIVE_SCHEMA_QUERY][0]["COLLATION_NAME"] = value
                with self.assertRaises(DatabaseConnectionError):
                    await get_live_schema(0, sql_executor=executor, state=ContextRuntimeState())


if __name__ == "__main__":
    unittest.main()
