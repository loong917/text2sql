"""Read-only SQL Server connectivity, TLS and least-privilege preflight."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

import pyodbc

from ..core.config import Settings
from ..core.production import is_production


def _connection_options(connection_string: str) -> dict[str, str]:
    options: dict[str, str] = {}
    for match in re.finditer(r"(?:^|;)\s*([^=;]+)=((?:\{[^}]*\})|[^;]*)", connection_string):
        key = match.group(1).strip().lower()
        value = match.group(2).strip().strip("{}").strip()
        options[key] = value
    return options


def database_configuration_errors(config: Settings) -> list[str]:
    options = _connection_options(config.mssql_conn_str)
    errors: list[str] = []
    driver = options.get("driver", "")
    if "odbc driver 18 for sql server" not in driver.lower():
        errors.append("MSSQL_CONN_STR must use ODBC Driver 18 for SQL Server")
    if is_production(config):
        encrypt = options.get("encrypt", "").lower()
        trust = options.get("trustservercertificate", "").lower()
        if encrypt not in {"yes", "true", "mandatory", "strict"}:
            errors.append("MSSQL_CONN_STR must enable TLS encryption")
        if trust in {"yes", "true"}:
            errors.append("TrustServerCertificate must be false in production")
    return errors


@dataclass(frozen=True)
class DatabasePreflightReport:
    success: bool
    driver: str
    database: str | None
    encryption: str
    trust_server_certificate: str
    read_only_principal: bool | None
    dangerous_permissions: tuple[str, ...]
    configuration_errors: tuple[str, ...]
    connection_encrypted: bool | None = None
    error_code: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_database_preflight(
    config: Settings, *, timeout_seconds: int = 8
) -> DatabasePreflightReport:
    options = _connection_options(config.mssql_conn_str)
    configuration_errors = database_configuration_errors(config)
    driver = options.get("driver", "unknown")
    installed = {item.lower() for item in pyodbc.drivers()}
    if driver.lower() not in installed:
        return DatabasePreflightReport(
            success=False,
            driver=driver,
            database=options.get("database"),
            encryption=options.get("encrypt", "unspecified"),
            trust_server_certificate=options.get("trustservercertificate", "unspecified"),
            read_only_principal=None,
            dangerous_permissions=(),
            configuration_errors=tuple(configuration_errors),
            error_code="ODBC_DRIVER_MISSING",
            error="configured ODBC driver is not installed",
        )
    try:
        connection = pyodbc.connect(config.mssql_conn_str, timeout=timeout_seconds)
        try:
            cursor = connection.cursor()
            connection_row = cursor.execute(
                "SELECT DB_NAME(), encrypt_option FROM sys.dm_exec_connections "
                "WHERE session_id = @@SPID"
            ).fetchone()
            database = str(connection_row[0])
            connection_encrypted = str(connection_row[1]).upper() == "TRUE"
            permissions = {
                str(row[0]).upper()
                for row in cursor.execute(
                    "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')"
                ).fetchall()
            }
        finally:
            connection.close()
    except pyodbc.Error as exc:
        sqlstate = str(exc.args[0]) if exc.args else "ODBC_ERROR"
        return DatabasePreflightReport(
            success=False,
            driver=driver,
            database=options.get("database"),
            encryption=options.get("encrypt", "unspecified"),
            trust_server_certificate=options.get("trustservercertificate", "unspecified"),
            read_only_principal=None,
            dangerous_permissions=(),
            configuration_errors=tuple(configuration_errors),
            error_code=sqlstate,
            error="SQL Server connection or permission probe failed; inspect server logs",
        )

    dangerous = tuple(
        sorted(
            permissions
            & {
                "ALTER",
                "CONTROL",
                "CREATE TABLE",
                "DELETE",
                "INSERT",
                "TAKE OWNERSHIP",
                "UPDATE",
            }
        )
    )
    read_only = not dangerous
    encrypted_for_environment = connection_encrypted or not is_production(config)
    success = not configuration_errors and read_only and encrypted_for_environment
    error_code = None
    error = None
    if not read_only:
        error_code = "DATABASE_PRINCIPAL_NOT_READ_ONLY"
        error = "database principal has write or control permissions"
    elif not encrypted_for_environment:
        error_code = "DATABASE_CONNECTION_NOT_ENCRYPTED"
        error = "SQL Server did not negotiate an encrypted connection"
    return DatabasePreflightReport(
        success=success,
        driver=driver,
        database=database,
        encryption=options.get("encrypt", "unspecified"),
        trust_server_certificate=options.get("trustservercertificate", "unspecified"),
        read_only_principal=read_only,
        dangerous_permissions=dangerous,
        configuration_errors=tuple(configuration_errors),
        connection_encrypted=connection_encrypted,
        error_code=error_code,
        error=error,
    )


def main() -> None:
    from ..core.config import load_settings

    report = run_database_preflight(load_settings())
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.success:
        raise SystemExit(1)
