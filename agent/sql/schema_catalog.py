"""Schema metadata verified against the actual, read-only SQLite database."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BUSINESS_TABLES = frozenset({"orders", "logistics", "products"})
_REQUIRED_COLUMNS = {
    "orders": frozenset({"order_id", "user_id", "tenant_id"}),
    "logistics": frozenset({"order_id"}),
    "products": frozenset({"sku_id"}),
}


@dataclass(frozen=True)
class SchemaCatalog:
    """Only catalogued business tables are available to SQL generation/validation.

    The mapping is derived from SQLite and checked against the maintained YAML
    catalog. Neither an LLM response nor a request can extend this allowlist.
    """

    columns: Mapping[str, frozenset[str]]
    status_codes: Mapping[str, Mapping[int, str]] = field(default_factory=dict)
    descriptions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    non_returnable: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_database(cls, database_path: str | Path, catalog_path: str | Path | None = None) -> SchemaCatalog:
        path = Path(database_path).resolve()
        if not path.is_file():
            raise ValueError(f"Business database does not exist: {path}")
        if catalog_path is None:
            catalog_path = path.parent / "schema_catalog.yaml"
        with Path(catalog_path).open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream) or {}
        declared_tables = document.get("tables", {})
        if not isinstance(declared_tables, dict):
            raise ValueError("Schema catalog must contain a tables mapping")

        actual: dict[str, frozenset[str]] = {}
        descriptions: dict[str, dict[str, str]] = {}
        non_returnable: dict[str, frozenset[str]] = {}
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            for table in BUSINESS_TABLES:
                declared = declared_tables.get(table)
                if not isinstance(declared, dict):
                    raise ValueError(f"Missing catalog table: {table}")
                rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
                if not rows:
                    raise ValueError(f"Missing database table: {table}")
                actual_columns = frozenset(str(row[1]).lower() for row in rows)
                if not _REQUIRED_COLUMNS[table] <= actual_columns:
                    raise ValueError(f"Missing authorization/key columns in {table}")
                definitions = declared.get("columns", {})
                if not isinstance(definitions, dict):
                    raise ValueError(f"Catalog columns for {table} must be a mapping")
                declared_columns = frozenset(str(name).lower() for name in definitions)
                if actual_columns != declared_columns:
                    raise ValueError(f"Catalog columns disagree with SQLite for {table}")
                actual[table] = actual_columns
                descriptions[table] = {
                    str(name).lower(): str(meta.get("description", ""))
                    for name, meta in definitions.items()
                    if isinstance(meta, dict)
                }
                non_returnable[table] = frozenset(
                    str(name).lower()
                    for name, meta in definitions.items()
                    if isinstance(meta, dict) and meta.get("returnable") is False
                )
        finally:
            connection.close()

        codes = document.get("status_codes", {})
        if not isinstance(codes, dict):
            raise ValueError("status_codes must be a mapping")
        status_codes: dict[str, dict[int, str]] = {}
        for table in ("orders", "logistics"):
            raw: Any = codes.get(table) or declared_tables[table].get("statuses", {})
            if not isinstance(raw, dict) or not raw:
                raise ValueError(f"Missing status dictionary for {table}")
            status_codes[table] = {int(key): str(value) for key, value in raw.items()}
        return cls(actual, status_codes, descriptions, non_returnable)

    @classmethod
    def from_mapping(
        cls,
        columns: Mapping[str, set[str] | frozenset[str] | list[str]],
        status_codes: Mapping[str, Mapping[int, str]] | None = None,
    ) -> SchemaCatalog:
        """Construct a small catalog for isolated tests or alternative stores."""
        normalized = {
            str(table).lower(): frozenset(str(column).lower() for column in names) for table, names in columns.items()
        }
        if set(normalized) - BUSINESS_TABLES:
            raise ValueError("Only documented business tables may be catalogued")
        for table, required in _REQUIRED_COLUMNS.items():
            if table in normalized and not required <= normalized[table]:
                raise ValueError(f"Missing authorization/key columns in {table}")
        return cls(
            normalized,
            status_codes or {},
            {},
            {"orders": frozenset({"user_id", "tenant_id"})},
        )

    def schema_summary(self, tables: set[str] | None = None) -> str:
        """Short, data-free schema summary for a SQL candidate provider."""
        chosen = set(self.columns) if tables is None else set(self.columns) & tables
        lines = [f"{table}({', '.join(sorted(self.columns[table]))})" for table in sorted(chosen)]
        lines.append("Join: orders.order_id = logistics.order_id; orders.sku_id = products.sku_id")
        for table in ("orders", "logistics"):
            if table in chosen and table in self.status_codes:
                mapping = ", ".join(f"{key}={value}" for key, value in sorted(self.status_codes[table].items()))
                lines.append(f"{table}.status: {mapping}")
        return "\n".join(lines)
