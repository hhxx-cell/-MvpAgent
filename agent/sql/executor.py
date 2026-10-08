"""SQLite execution with read-only URI, authorizer, deadline and row cap."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .validator import ValidatedQuery, is_validated_query


class SQLExecutionError(RuntimeError):
    """Safe error category; raw SQLite text must never be sent to users."""

    def __init__(self, code: str, *, correctable: bool = False):
        super().__init__(code)
        self.code = code
        self.correctable = correctable


def _readonly_uri(database_path: str | Path) -> str:
    raw = str(database_path)
    if raw.startswith("file:"):
        parsed = urlsplit(raw)
        query = parse_qs(parsed.query)
        if query.get("mode") != ["ro"] or len(query.get("mode", [])) != 1:
            raise SQLExecutionError("DATABASE_NOT_READ_ONLY")
        return raw
    path = Path(database_path).resolve()
    if not path.is_file():
        raise SQLExecutionError("DATABASE_UNAVAILABLE")
    return f"{path.as_uri()}?mode=ro"


def _sqlite_authorizer(
    action: int,
    argument_1: str | None,
    argument_2: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    del argument_2, database, trigger
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and argument_1 in {"orders", "logistics", "products"}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK  # Function names were allowlisted in the AST.
    return sqlite3.SQLITE_DENY


def _classify_error(error: sqlite3.DatabaseError, *, expired: bool) -> SQLExecutionError:
    if expired or "interrupted" in str(error).lower():
        return SQLExecutionError("SQL_TIMEOUT")
    message = str(error).lower()
    if "no such function" in message:
        return SQLExecutionError("SQL_DIALECT_ERROR", correctable=True)
    if "no such column" in message or "ambiguous column" in message:
        return SQLExecutionError("SQL_COLUMN_ERROR", correctable=True)
    if "misuse of aggregate" in message or "group by" in message:
        return SQLExecutionError("SQL_GROUPING_ERROR", correctable=True)
    return SQLExecutionError("SQL_EXECUTION_FAILED")


def execute_sql(
    validated: ValidatedQuery,
    database_path: str | Path,
    *,
    timeout_seconds: float = 5.0,
) -> list[dict[str, object]]:
    """Execute a validated query and return at most its row cap as named rows."""
    if not is_validated_query(validated):
        raise TypeError("execute_sql requires a ValidatedQuery")
    if not 0 < timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be within (0, 30]")
    deadline = time.monotonic() + timeout_seconds
    expired = False

    def progress() -> int:
        nonlocal expired
        if time.monotonic() >= deadline:
            expired = True
            return 1
        return 0

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _readonly_uri(database_path),
            uri=True,
            timeout=min(2.0, timeout_seconds),
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_authorizer(_sqlite_authorizer)
        connection.set_progress_handler(progress, 1000)
        cursor = connection.execute(validated.sql, validated.parameters)
        if cursor.description is None:
            raise SQLExecutionError("SQL_EXECUTION_FAILED")
        rows = cursor.fetchmany(validated.max_rows + 1)
        if len(rows) > validated.max_rows:
            rows = rows[: validated.max_rows]
        return [dict(row) for row in rows]
    except sqlite3.DatabaseError as error:
        raise _classify_error(error, expired=expired) from error
    finally:
        if connection is not None:
            connection.close()
