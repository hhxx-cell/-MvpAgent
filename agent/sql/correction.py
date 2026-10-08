"""Bounded correction loop for execution errors that are safe to repair."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp, parse_one

from .executor import SQLExecutionError, execute_sql
from .generator import SQLCandidate
from .schema_catalog import SchemaCatalog
from .validator import ValidatedQuery, validate_sql


@dataclass(frozen=True)
class SQLRunResult:
    rows: list[dict[str, object]]
    query: ValidatedQuery
    corrections: int


Corrector = Callable[[str, str, str], SQLCandidate | str | None]


def deterministic_safe_corrector(previous_sql: str, safe_error_code: str, catalog: SchemaCatalog) -> str | None:
    """Repair only known-safe alias and SQLite date-truncation mistakes.

    This function never expands the table or column allowlist. Its output is
    still revalidated and reauthorized by `run_sql_with_corrections`.
    """
    if safe_error_code not in {"SQL_COLUMN_ERROR", "SQL_DIALECT_ERROR"}:
        return None
    try:
        tree = parse_one(previous_sql, read="sqlite")
    except Exception:
        return None
    if tree is None:
        return None

    if safe_error_code == "SQL_COLUMN_ERROR":
        tables = list(tree.find_all(exp.Table))
        order_aliases = [table.alias_or_name for table in tables if table.name.lower() == "orders"]
        has_logistics = any(table.name.lower() == "logistics" for table in tables)
        if len(order_aliases) != 1 or not has_logistics:
            return None
        if "order_id" not in catalog.columns.get("orders", frozenset()):
            return None
        changed = False
        for column in tree.find_all(exp.Column):
            if column.name.lower() == "order_id" and not column.table:
                column.set("table", exp.to_identifier(order_aliases[0]))
                changed = True
        return tree.sql(dialect="sqlite") if changed else None

    date_formats = {
        "MONTH": "%Y-%m-01",
        "YEAR": "%Y-01-01",
        "DAY": "%Y-%m-%d",
    }
    changed = False

    def convert(node: exp.Expression) -> exp.Expression:
        nonlocal changed
        if not isinstance(node, exp.DateTrunc):
            return node
        unit = node.args.get("unit")
        if not isinstance(unit, exp.Literal):
            return node
        date_format = date_formats.get(str(unit.this).upper())
        if date_format is None or node.this is None:
            return node
        changed = True
        return exp.Anonymous(
            this="STRFTIME",
            expressions=[exp.Literal.string(date_format), node.this.copy()],
        )

    repaired = tree.transform(convert)
    return repaired.sql(dialect="sqlite") if changed else None


def run_sql_with_corrections(
    candidate: SQLCandidate | str,
    principal_id: str,
    tenant_id: str,
    catalog: SchemaCatalog,
    database_path: str | Path,
    *,
    params: Mapping[str, object] | None = None,
    corrector: Corrector | None = None,
    max_corrections: int = 2,
    deadline_seconds: float = 10.0,
    max_rows: int = 200,
) -> SQLRunResult:
    """Revalidate every corrected candidate; never correct safety failures.

    The callback arguments are `(previous_sql, safe_error_code, linked_schema)`.
    It never receives the raw SQLite error or trusted authorization bindings.
    """
    if not 0 <= max_corrections <= 2:
        raise ValueError("max_corrections must be between zero and two")
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    stop_at = time.monotonic() + deadline_seconds
    current = candidate if isinstance(candidate, SQLCandidate) else SQLCandidate(candidate, params or {})
    for correction_count in range(max_corrections + 1):
        validated = validate_sql(
            current.sql,
            principal_id,
            catalog,
            current.parameters,
            tenant_id=tenant_id,
            max_rows=max_rows,
        )
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            raise SQLExecutionError("SQL_TIMEOUT")
        try:
            rows = execute_sql(validated, database_path, timeout_seconds=min(remaining, 30.0))
            return SQLRunResult(rows, validated, correction_count)
        except SQLExecutionError as error:
            if not error.correctable or correction_count >= max_corrections:
                raise
            replacement = (
                corrector(current.sql, error.code, catalog.schema_summary())
                if corrector is not None
                else deterministic_safe_corrector(current.sql, error.code, catalog)
            )
            if replacement is None:
                raise
            current = (
                replacement
                if isinstance(replacement, SQLCandidate)
                else SQLCandidate(replacement, current.parameters, source="correction")
            )
    raise SQLExecutionError("SQL_EXECUTION_FAILED")  # Defensive, loop always returns or raises.
