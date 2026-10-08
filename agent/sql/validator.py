"""Fail-closed SQLGlot validation and structural object authorization.

Every occurrence of a protected base table is replaced with a scoped derived
table. This also covers CTEs, UNION branches, nested SELECTs and a direct
logistics read; authorization does not depend on a model remembering a WHERE.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .schema_catalog import SchemaCatalog

_VALIDATION_TOKEN = object()
_RESERVED_PARAMS = frozenset({"auth_principal_id", "auth_tenant_id"})
_ALLOWED_FUNCTIONS = frozenset(
    {
        "ABS",
        "AND",
        "AVG",
        "CAST",
        "COALESCE",
        "COUNT",
        "DATE",
        "DATETIME",
        "DATE_TRUNC",
        "EXISTS",
        "IFNULL",
        "LOWER",
        "MAX",
        "MIN",
        "NOT",
        "NULLIF",
        "OR",
        "ROUND",
        "STRFTIME",
        "SUBSTR",
        "SUM",
        "TIME_TO_STR",
        "TS_OR_DS_TO_TIMESTAMP",
        "UPPER",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "alter",
        "analyze",
        "attach",
        "command",
        "copy",
        "create",
        "delete",
        "describe",
        "detach",
        "drop",
        "explain",
        "grant",
        "insert",
        "into",
        "merge",
        "pragma",
        "replace",
        "revoke",
        "set",
        "show",
        "transaction",
        "truncate",
        "truncatetable",
        "update",
        "use",
        "vacuum",
    }
)


class SQLValidationError(ValueError):
    """A candidate was rejected before it could reach SQLite."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, init=False)
class ValidatedQuery:
    sql: str
    parameters: Mapping[str, object]
    max_rows: int

    def __init__(
        self,
        sql: str,
        parameters: Mapping[str, object],
        max_rows: int,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _VALIDATION_TOKEN:
            raise TypeError("ValidatedQuery can only be created by validate_sql")
        object.__setattr__(self, "sql", sql)
        object.__setattr__(self, "parameters", dict(parameters))
        object.__setattr__(self, "max_rows", max_rows)


def _parse_one_statement(sql: str) -> exp.Expression:
    try:
        statements = parse(sql, read="sqlite")
    except (ParseError, ValueError) as exc:
        raise SQLValidationError("SQL_PARSE_ERROR", "SQL syntax is invalid") from exc
    if len(statements) != 1 or statements[0] is None:
        raise SQLValidationError("SQL_REJECTED", "Exactly one SQL statement is required")
    return statements[0]


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.upper()
    return function.sql_name().upper()


def _validate_tree(
    tree: exp.Expression,
    catalog: SchemaCatalog,
    *,
    candidate: bool,
) -> set[str]:
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SQLValidationError("SQL_REJECTED", "Only SELECT queries are permitted")

    cte_names: set[str] = set()
    derived_columns: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        name = cte.alias_or_name.lower()
        if not name or name in catalog.columns or name in cte_names:
            raise SQLValidationError("SQL_REJECTED", "Invalid or shadowed CTE name")
        cte_names.add(name)
        if not isinstance(cte.this, (exp.Select, exp.Union)):
            raise SQLValidationError("SQL_REJECTED", "CTE must contain a SELECT")
    for alias in tree.find_all(exp.Alias):
        if alias.alias:
            derived_columns.add(alias.alias.lower())
    for cte in tree.find_all(exp.CTE):
        table_alias = cte.args.get("alias")
        if table_alias is not None:
            derived_columns.update(identifier.name.lower() for identifier in table_alias.args.get("columns") or [])

    aliases: dict[str, str | None] = {}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if table.db or table.catalog or table.args.get("pivots"):
            raise SQLValidationError("SQL_REJECTED", "Qualified or pivoted tables are not permitted")
        if name not in catalog.columns and name not in cte_names:
            raise SQLValidationError("SQL_REJECTED", "Unknown table")
        alias_name = table.alias_or_name.lower()
        aliases[alias_name] = name if name in catalog.columns else None
    for subquery in tree.find_all(exp.Subquery):
        if subquery.alias:
            aliases[subquery.alias.lower()] = None
    for cte_name in cte_names:
        aliases.setdefault(cte_name, None)

    known_columns = set().union(*catalog.columns.values()) if catalog.columns else set()
    known_columns |= derived_columns
    for node in tree.walk():
        if node.key in _FORBIDDEN_KEYS or isinstance(node, (exp.DDL, exp.DML, exp.Command)):
            raise SQLValidationError("SQL_REJECTED", "Non-read-only SQL is not permitted")
        if isinstance(node, (exp.Except, exp.Intersect)):
            raise SQLValidationError("SQL_REJECTED", "Unsupported set operation")
        if isinstance(node, exp.Union) and not all(
            isinstance(branch, (exp.Select, exp.Union)) for branch in (node.this, node.expression)
        ):
            raise SQLValidationError("SQL_REJECTED", "Every UNION branch must be a SELECT")
        if isinstance(node, exp.Subquery) and not isinstance(node.this, (exp.Select, exp.Union)):
            raise SQLValidationError("SQL_REJECTED", "Subquery must contain a SELECT")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLValidationError("SQL_REJECTED", "Recursive CTEs are not permitted")
        if isinstance(node, exp.Star):
            raise SQLValidationError("SQL_REJECTED", "SELECT star is not permitted")
        if isinstance(node, exp.Func) and _function_name(node) not in _ALLOWED_FUNCTIONS:
            raise SQLValidationError("SQL_REJECTED", "SQL function is not permitted")
        if isinstance(node, exp.Select):
            if not node.expressions or node.args.get("into") or node.args.get("locks"):
                raise SQLValidationError("SQL_REJECTED", "Invalid SELECT shape")
            from_clause = node.args.get("from_")
            if from_clause and not isinstance(from_clause.this, (exp.Table, exp.Subquery)):
                raise SQLValidationError("SQL_REJECTED", "Unsupported FROM source")
        if isinstance(node, exp.Join):
            if not isinstance(node.this, (exp.Table, exp.Subquery)):
                raise SQLValidationError("SQL_REJECTED", "Unsupported JOIN source")
            if node.args.get("method") == "NATURAL" or node.args.get("kind") == "CROSS":
                raise SQLValidationError("SQL_REJECTED", "Unbounded joins are not permitted")
            if not node.args.get("on") and not node.args.get("using"):
                raise SQLValidationError("SQL_REJECTED", "Join condition is required")
        if isinstance(node, exp.Column):
            column = node.name.lower()
            qualifier = node.table.lower()
            if not column or column not in known_columns:
                raise SQLValidationError("SQL_REJECTED", "Unknown column")
            if qualifier:
                if qualifier not in aliases:
                    raise SQLValidationError("SQL_REJECTED", "Unknown column qualifier")
                source = aliases[qualifier]
                if source and column not in catalog.columns[source]:
                    raise SQLValidationError("SQL_REJECTED", "Column is not in its table")
    if candidate:
        prohibited = set().union(*catalog.non_returnable.values()) if catalog.non_returnable else set()
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if any(column.name.lower() in prohibited for column in projection.find_all(exp.Column)):
                    raise SQLValidationError("SQL_REJECTED", "Sensitive field cannot be projected")
    return cte_names


def _protected_table(table: exp.Table, catalog: SchemaCatalog) -> exp.Subquery:
    name = table.name.lower()
    alias = table.alias_or_name
    if name == "orders":
        source_alias = "_guard_orders"
        fields = ", ".join(f"{source_alias}.{column}" for column in sorted(catalog.columns[name]))
        secure_sql = (
            f"SELECT {fields} FROM orders AS {source_alias} "
            f"WHERE {source_alias}.user_id = :auth_principal_id "
            f"AND {source_alias}.tenant_id = :auth_tenant_id"
        )
    else:
        source_alias = "_guard_logistics"
        owner_alias = "_guard_owner"
        fields = ", ".join(f"{source_alias}.{column}" for column in sorted(catalog.columns[name]))
        secure_sql = (
            f"SELECT {fields} FROM logistics AS {source_alias} "
            f"JOIN orders AS {owner_alias} "
            f"ON {owner_alias}.order_id = {source_alias}.order_id "
            f"WHERE {owner_alias}.user_id = :auth_principal_id "
            f"AND {owner_alias}.tenant_id = :auth_tenant_id"
        )
    secure_select = _parse_one_statement(secure_sql)
    return exp.Subquery(
        this=secure_select,
        alias=exp.TableAlias(this=exp.to_identifier(alias)),
    )


def validate_sql(
    candidate: str,
    principal_id: str,
    catalog: SchemaCatalog,
    params: Mapping[str, object] | None = None,
    *,
    tenant_id: str | None,
    max_rows: int = 200,
) -> ValidatedQuery:
    """Validate and bind one read-only, principal-scoped SQLite query.

    `params` contains only candidate placeholders. The two authorization
    bindings are injected by this function and cannot be provided by a model.
    """
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 20_000:
        raise SQLValidationError("SQL_REJECTED", "SQL candidate is empty or too large")
    if not principal_id or not tenant_id:
        raise SQLValidationError("FORBIDDEN_RESOURCE", "Trusted user and tenant are required")
    if not isinstance(max_rows, int) or not 1 <= max_rows <= 1000:
        raise SQLValidationError("SQL_REJECTED", "Invalid row limit")
    if "--" in candidate or "/*" in candidate or "*/" in candidate or "\x00" in candidate:
        raise SQLValidationError("SQL_REJECTED", "Comments and control characters are not permitted")
    tree = _parse_one_statement(candidate)
    cte_names = _validate_tree(tree, catalog, candidate=True)
    user_params = dict(params or {})
    if any(not isinstance(key, str) or not key.isidentifier() for key in user_params):
        raise SQLValidationError("SQL_REJECTED", "Invalid SQL parameter name")
    if set(user_params) & _RESERVED_PARAMS:
        raise SQLValidationError("SQL_REJECTED", "Reserved authorization parameter")
    placeholders = {str(item.this or "") for item in tree.find_all(exp.Placeholder)}
    if "" in placeholders or placeholders != set(user_params):
        raise SQLValidationError("SQL_REJECTED", "SQL parameters do not match bindings")
    if any(not isinstance(value, (str, int, float, bytes, type(None))) for value in user_params.values()):
        raise SQLValidationError("SQL_REJECTED", "Unsupported SQL parameter type")

    # Collect before mutation, so generated inner tables are never rewritten a
    # second time. The SQL is reparsed and statically checked after rewriting.
    original_tables = list(tree.find_all(exp.Table))
    for table in original_tables:
        if table.name.lower() in cte_names:
            continue
        if table.name.lower() in {"orders", "logistics"}:
            table.replace(_protected_table(table, catalog))
    tree.limit(max_rows, copy=False)
    sql = tree.sql(dialect="sqlite")
    rewritten = _parse_one_statement(sql)
    _validate_tree(rewritten, catalog, candidate=False)
    parameters = {
        **user_params,
        "auth_principal_id": principal_id,
        "auth_tenant_id": tenant_id,
    }
    return ValidatedQuery(sql, parameters, max_rows, _token=_VALIDATION_TOKEN)


def is_validated_query(query: object) -> bool:
    """Internal provenance check used by the executor."""
    return isinstance(query, ValidatedQuery)
