"""Portable fingerprint of a SQLite database's logical tables and rows.

SQLite file bytes depend on page layout, SQLite version, and insertion order.
Golden fixtures need to identify the data itself, so this digest ignores those
physical details while retaining table definitions and every row value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _encode_value(value: Any) -> list[str | int]:
    if value is None:
        return ["null", ""]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def logical_database_hash(path: Path) -> str:
    """Hash user tables, schema metadata, and rows independent of row order."""
    uri = path.resolve().as_uri() + "?mode=ro"
    tables: list[dict[str, Any]] = []
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            identifier = '"' + name.replace('"', '""') + '"'
            columns = [list(row) for row in connection.execute(f"PRAGMA table_info({identifier})")]
            foreign_keys = sorted(
                (list(row) for row in connection.execute(f"PRAGMA foreign_key_list({identifier})")),
                key=_canonical_json,
            )
            rows = sorted(
                ([_encode_value(value) for value in row] for row in connection.execute(f"SELECT * FROM {identifier}")),
                key=_canonical_json,
            )
            tables.append({"name": name, "columns": columns, "foreign_keys": foreign_keys, "rows": rows})
    payload = _canonical_json({"format": 1, "tables": tables}).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
