"""Effective permissions at every SQL Server scope must be read-only."""

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import pyodbc

from text2sql.core.config import load_settings
from text2sql.core.production import production_configuration_errors
from text2sql.infrastructure.database_preflight import run_database_preflight


class PermissionCursor:
    def __init__(self, denied_scope=None, denied_permission="INSERT"):
        self.denied_scope = denied_scope
        self.denied_permission = denied_permission
        self.query = ""

    def execute(self, query):
        self.query = query
        return self

    def fetchone(self):
        return ("test", "TRUE")

    def fetchall(self):
        scope = next(
            name for name in ("DATABASE", "SCHEMA", "OBJECT", "SERVER") if f"'{name}'" in self.query
        )
        allowed = "CONNECT SQL" if scope == "SERVER" else "SELECT"
        return [(allowed,)] + ([(self.denied_permission,)] if scope == self.denied_scope else [])


class DatabasePreflightTests(unittest.TestCase):
    def config(self):
        return replace(
            load_settings(),
            app_env="production",
            mssql_conn_str=(
                "Driver={ODBC Driver 18 for SQL Server};Server=test;Database=test;"
                "UID=reader;PWD=test;Encrypt=yes;TrustServerCertificate=no"
            ),
        )

    def probe(self, cursor):
        connection = Mock(cursor=Mock(return_value=cursor))
        with (
            patch(
                "text2sql.infrastructure.database_preflight.pyodbc.drivers",
                return_value=["ODBC Driver 18 for SQL Server"],
            ),
            patch(
                "text2sql.infrastructure.database_preflight.pyodbc.connect", return_value=connection
            ),
        ):
            report = run_database_preflight(self.config())
        connection.close.assert_called_once()
        return report

    def test_read_only_effective_permissions_and_negotiated_tls_pass(self):
        report = self.probe(PermissionCursor())
        self.assertTrue(report.success)
        self.assertTrue(report.connection_encrypted)
        self.assertEqual(report.dangerous_permissions, ())

    def test_write_or_execute_permission_at_any_scope_blocks_release(self):
        for scope, permission in (
            ("DATABASE", "CONTROL"),
            ("SCHEMA", "INSERT"),
            ("OBJECT", "EXECUTE"),
            ("SERVER", "IMPERSONATE ANY LOGIN"),
        ):
            with self.subTest(scope=scope):
                report = self.probe(PermissionCursor(scope, permission))
                self.assertFalse(report.success)
                self.assertFalse(report.read_only_principal)
                self.assertIn(f"{scope}:{permission}", report.dangerous_permissions)

    def test_permission_probe_failure_is_not_read_only_evidence(self):
        cursor = Mock()
        cursor.execute.side_effect = pyodbc.Error("42000", "private database details")
        report = self.probe(cursor)
        self.assertFalse(report.success)
        self.assertIsNone(report.read_only_principal)
        self.assertNotIn("private database details", report.error)

    def test_production_quality_floors_cannot_be_lowered(self):
        config = replace(
            self.config(),
            production_eval_min_cases=1,
            production_retrieval_test_min_cases=1,
            eval_min_pass_rate=0.1,
        )
        errors = production_configuration_errors(config)
        for name in (
            "PRODUCTION_EVAL_MIN_CASES",
            "PRODUCTION_RETRIEVAL_TEST_MIN_CASES",
            "EVAL_MIN_PASS_RATE",
        ):
            self.assertTrue(any(error.startswith(name) for error in errors))
