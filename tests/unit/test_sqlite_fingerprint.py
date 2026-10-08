"""Golden data identity must be independent of SQLite's physical layout."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from eval.sqlite_fingerprint import logical_database_hash


def _make_database(path: Path, rows: list[tuple[str, int, bytes]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (item_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL, tag BLOB)")
        connection.executemany("INSERT INTO items (item_id, quantity, tag) VALUES (?, ?, ?)", rows)


def test_logical_hash_ignores_insert_order_but_detects_changed_data(tmp_path: Path) -> None:
    rows = [("A", 1, b"\x00"), ("B", 2, b"\xff"), ("C", 3, b"")]
    first = tmp_path / "first.db"
    reordered = tmp_path / "reordered.db"
    changed = tmp_path / "changed.db"
    _make_database(first, rows)
    _make_database(reordered, list(reversed(rows)))
    _make_database(changed, [("A", 1, b"\x00"), ("B", 9, b"\xff"), ("C", 3, b"")])

    assert hashlib.sha256(first.read_bytes()).digest() != hashlib.sha256(reordered.read_bytes()).digest()
    assert logical_database_hash(first) == logical_database_hash(reordered)
    assert logical_database_hash(first) != logical_database_hash(changed)
