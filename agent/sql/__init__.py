"""Read-only, principal-scoped SQL access for the agent."""

from .correction import SQLRunResult, deterministic_safe_corrector, run_sql_with_corrections
from .executor import SQLExecutionError, execute_sql
from .generator import SQLCandidate, UnsupportedSQLQuestion, generate_sql
from .schema_catalog import SchemaCatalog
from .validator import SQLValidationError, ValidatedQuery, validate_sql

__all__ = [
    "SQLCandidate",
    "SQLExecutionError",
    "SQLRunResult",
    "SQLValidationError",
    "SchemaCatalog",
    "UnsupportedSQLQuestion",
    "ValidatedQuery",
    "deterministic_safe_corrector",
    "execute_sql",
    "generate_sql",
    "run_sql_with_corrections",
    "validate_sql",
]
