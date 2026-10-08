"""Verify the reproducible synthetic data assets in data_manifest.json.

This command is deliberately read-only. Run it after generate_seed_data.py or
before starting the application::

    python scripts/verify_data_manifest.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

REQUIRED_FILES = {
    "database/ecommerce.db",
    "database/seeds/orders.jsonl",
    "database/seeds/rejected_orders.jsonl",
    "eval/logs.jsonl",
}
ORDER_COLUMNS = (
    "order_id",
    "user_id",
    "tenant_id",
    "sku_id",
    "quantity",
    "amount_cents",
    "status",
    "created_at",
    "updated_at",
    "currency",
)
PARAGRAPH_ID = re.compile(r"\[p-\d{3,}\]")


class Verification:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.errors: list[str] = []

    def fail(self, message: str) -> None:
        self.errors.append(message)

    def asset_path(self, relative: str) -> Path | None:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path == Path("."):
            self.fail(f"Unsafe asset path: {relative!r}")
            return None
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            self.fail(f"Asset escapes repository root: {relative!r}")
            return None
        return resolved

    def load_json(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.fail(f"Cannot read {path}: {exc}")
            return None
        if not isinstance(value, dict):
            self.fail(f"Manifest must contain a JSON object: {path}")
            return None
        return value

    def jsonl(self, relative: str) -> list[dict[str, Any]]:
        path = self.asset_path(relative)
        if path is None:
            return []
        result: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        self.fail(f"{relative}:{line_number}: blank JSONL line")
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        self.fail(f"{relative}:{line_number}: invalid JSON: {exc}")
                        continue
                    if not isinstance(value, dict):
                        self.fail(f"{relative}:{line_number}: expected JSON object")
                        continue
                    result.append(value)
        except (OSError, UnicodeError) as exc:
            self.fail(f"Cannot read {relative}: {exc}")
        return result

    def check_metadata(self, manifest: dict[str, Any]) -> None:
        if isinstance(manifest.get("seed"), bool) or not isinstance(manifest.get("seed"), int):
            self.fail("Manifest seed must be an integer")
        if not isinstance(manifest.get("version"), (str, int)) or not str(manifest.get("version")):
            self.fail("Manifest version is missing")
        if manifest.get("timezone") != "Asia/Shanghai":
            self.fail("Manifest timezone must be Asia/Shanghai")
        if manifest.get("currency") != "CNY":
            self.fail("Manifest currency must be CNY")

    def check_files(self, manifest: dict[str, Any]) -> None:
        files = manifest.get("files")
        if not isinstance(files, dict):
            self.fail("Manifest files must map relative paths to metadata")
            return
        missing = REQUIRED_FILES - set(files)
        for relative in sorted(missing):
            self.fail(f"Required asset missing from manifest: {relative}")
        for relative, metadata in files.items():
            if not isinstance(relative, str) or not isinstance(metadata, dict):
                self.fail(f"Invalid files entry: {relative!r}")
                continue
            path = self.asset_path(relative)
            if path is None:
                continue
            if not path.is_file():
                self.fail(f"Asset does not exist: {relative}")
                continue
            size = path.stat().st_size
            if isinstance(metadata.get("size"), bool) or metadata.get("size") != size:
                self.fail(f"Size mismatch: {relative} (expected {metadata.get('size')}, found {size})")
            expected_hash = metadata.get("sha256")
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                self.fail(f"Invalid SHA-256 in manifest: {relative}")
            else:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected_hash:
                    self.fail(f"SHA-256 mismatch: {relative}")
            if relative.endswith(".jsonl"):
                records = self.jsonl(relative)
                expected_records = metadata.get("records")
                if isinstance(expected_records, bool) or not isinstance(expected_records, int):
                    self.fail(f"JSONL record count missing: {relative}")
                elif len(records) != expected_records:
                    self.fail(
                        f"JSONL record count mismatch: {relative} (expected {expected_records}, found {len(records)})"
                    )

    def check_orders(self, manifest: dict[str, Any]) -> None:
        raw = self.jsonl("database/seeds/orders.jsonl")
        rejected = self.jsonl("database/seeds/rejected_orders.jsonl")
        valid_ids = manifest.get("valid_order_ids")
        if not isinstance(valid_ids, list) or not all(isinstance(item, str) for item in valid_ids):
            self.fail("Manifest valid_order_ids must be a list of order IDs")
            return
        if len(valid_ids) != len(set(valid_ids)) or valid_ids != sorted(valid_ids):
            self.fail("Manifest valid_order_ids must be sorted and unique")
        valid_count = manifest.get("valid_orders")
        rejected_count = manifest.get("rejected_orders")
        if isinstance(valid_count, bool) or not isinstance(valid_count, int):
            self.fail("Manifest valid_orders must be an integer")
        elif len(valid_ids) != valid_count:
            self.fail(f"Valid order ID count mismatch: {len(valid_ids)} != {valid_count}")
        if isinstance(rejected_count, bool) or not isinstance(rejected_count, int):
            self.fail("Manifest rejected_orders must be an integer")
        elif len(rejected) != rejected_count:
            self.fail(f"Rejected order count mismatch: {len(rejected)} != {rejected_count}")
        if isinstance(valid_count, int) and isinstance(rejected_count, int):
            if len(raw) != valid_count + rejected_count:
                self.fail(
                    f"Raw orders should equal valid plus rejected: {len(raw)} != {valid_count} + {rejected_count}"
                )
        if type(manifest.get("raw_orders")) is not int or manifest["raw_orders"] != len(raw):
            self.fail(f"Raw order count mismatch: {len(raw)} != {manifest.get('raw_orders')}")
        rejected_lines: set[int] = set()
        for index, item in enumerate(rejected, 1):
            source_line = item.get("source_line")
            if type(source_line) is not int or not 1 <= source_line <= len(raw):
                self.fail(f"Rejected order {index} has invalid source_line")
                continue
            if source_line in rejected_lines:
                self.fail(f"Rejected source line listed twice: {source_line}")
            rejected_lines.add(source_line)
            if not isinstance(item.get("reason"), str) or not item["reason"]:
                self.fail(f"Rejected source line {source_line} has no reason")
            if item.get("record") != raw[source_line - 1]:
                self.fail(f"Rejected record differs from raw source line {source_line}")
        expected_anomaly_samples = [
            {
                "sample_id": f"orders.jsonl:{item.get('source_line')}",
                "source_line": item.get("source_line"),
                "order_id": item["record"].get("order_id") if isinstance(item.get("record"), dict) else None,
                "reason": item.get("reason"),
            }
            for item in rejected
        ]
        if manifest.get("anomaly_samples") != expected_anomaly_samples:
            self.fail("Manifest anomaly_samples differ from rejected_orders.jsonl")
        accepted = [record for line, record in enumerate(raw, 1) if line not in rejected_lines]
        accepted_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in accepted:
            order_id = record.get("order_id")
            if isinstance(order_id, str):
                accepted_by_id[order_id].append(record)
        if sorted(accepted_by_id) != valid_ids or len(accepted) != len(valid_ids):
            self.fail("Accepted raw order IDs differ from manifest valid_order_ids")
        for order_id in valid_ids:
            if len(accepted_by_id.get(order_id, [])) != 1:
                self.fail(f"Valid order must appear once among accepted raw rows: {order_id}")

        db_path = self.asset_path("database/ecommerce.db")
        if db_path is None or not db_path.is_file():
            return
        try:
            connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            try:
                self._check_database(connection, manifest, set(valid_ids), accepted_by_id)
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self.fail(f"Cannot verify SQLite database: {exc}")

    def _check_database(
        self,
        connection: sqlite3.Connection,
        manifest: dict[str, Any],
        valid_ids: set[str],
        accepted_by_id: dict[str, list[dict[str, Any]]],
    ) -> None:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ("orders", "logistics", "products"):
            if table not in tables:
                self.fail(f"SQLite table missing: {table}")
        if not {"orders", "logistics", "products"}.issubset(tables):
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
        missing_columns = set(ORDER_COLUMNS) - columns
        if missing_columns:
            self.fail(f"SQLite orders columns missing: {sorted(missing_columns)}")
            return
        orders = [dict(row) for row in connection.execute("SELECT " + ", ".join(ORDER_COLUMNS) + " FROM orders")]
        db_ids = [row["order_id"] for row in orders]
        if len(db_ids) != len(set(db_ids)):
            self.fail("SQLite orders contains duplicate order IDs")
        if set(db_ids) != valid_ids:
            missing = sorted(valid_ids - set(db_ids))
            extra = sorted(set(db_ids) - valid_ids)
            self.fail(f"SQLite order IDs differ from manifest (missing={missing[:8]}, extra={extra[:8]})")
        orders_by_id = {row["order_id"]: row for row in orders}
        demo_orders = manifest.get("demo_orders")
        if not isinstance(demo_orders, dict) or not demo_orders:
            self.fail("Manifest demo_orders must map users to their sample order IDs")
        else:
            for user_id, order_id in demo_orders.items():
                order = orders_by_id.get(order_id)
                if order is None or order["user_id"] != user_id:
                    self.fail(f"Demo order is not owned by its listed user: {user_id} -> {order_id}")
        for row in orders:
            candidates = accepted_by_id.get(row["order_id"], [])
            if not any(
                all(candidate.get(column) == row[column] for column in ORDER_COLUMNS) for candidate in candidates
            ):
                self.fail(f"SQLite order differs from accepted JSONL row: {row['order_id']}")
            if row["currency"] != "CNY":
                self.fail(f"Unexpected order currency: {row['order_id']}")
        for table, key in (("products", "product_count"), ("logistics", "logistics_count")):
            expected = manifest.get(key)
            actual = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if type(expected) is not int or expected != actual:
                self.fail(f"SQLite {table} count mismatch: {actual} != {expected}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            self.fail(f"SQLite foreign key violations: {len(foreign_key_errors)}")
        missing_products = connection.execute(
            "SELECT DISTINCT o.sku_id FROM orders AS o "
            "LEFT JOIN products AS p ON p.sku_id = o.sku_id "
            "WHERE p.sku_id IS NULL"
        ).fetchall()
        if missing_products:
            self.fail(f"Orders refer to missing products: {[row[0] for row in missing_products[:8]]}")
        missing_orders = connection.execute(
            "SELECT DISTINCT l.order_id FROM logistics AS l "
            "LEFT JOIN orders AS o ON o.order_id = l.order_id "
            "WHERE o.order_id IS NULL"
        ).fetchall()
        if missing_orders:
            self.fail(f"Logistics refer to missing orders: {[row[0] for row in missing_orders[:8]]}")
        logistics_order_ids = [row[0] for row in connection.execute("SELECT order_id FROM logistics")]
        if len(logistics_order_ids) != len(set(logistics_order_ids)) or set(logistics_order_ids) != valid_ids:
            self.fail("SQLite logistics must contain one row for every valid order")

    def check_knowledge(self, manifest: dict[str, Any]) -> None:
        knowledge_dir = self.asset_path("knowledge/source")
        if knowledge_dir is None or not knowledge_dir.is_dir():
            self.fail("Knowledge source directory is missing")
            return
        documents = sorted(knowledge_dir.glob("*.md"))
        expected_count = manifest.get("knowledge_document_count")
        if isinstance(expected_count, bool) or not isinstance(expected_count, int):
            self.fail("Manifest knowledge_document_count must be an integer")
        elif len(documents) != expected_count:
            self.fail(f"Knowledge document count mismatch: {len(documents)} != {expected_count}")
        files = manifest.get("files", {})
        if isinstance(files, dict):
            for path in documents:
                relative = path.relative_to(self.root).as_posix()
                if relative not in files:
                    self.fail(f"Knowledge document missing from manifest: {relative}")
        versions: dict[str, set[tuple[str, int]]] = defaultdict(set)
        current_versions: dict[str, list[tuple[str, int]]] = defaultdict(list)
        topic_counts: dict[str, int] = defaultdict(int)
        for path in documents:
            relative = path.relative_to(self.root).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.fail(f"Cannot read {relative}: {exc}")
                continue
            if not content.startswith("---\n"):
                self.fail(f"Knowledge metadata frontmatter missing: {relative}")
                continue
            parts = content.split("---\n", 2)
            if len(parts) < 3:
                self.fail(f"Knowledge metadata frontmatter is unclosed: {relative}")
                continue
            metadata: dict[str, str] = {}
            for line in parts[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata[key.strip()] = value.strip().strip("\"'")
            for key in ("policy_key", "effective_date", "version", "title", "category"):
                if not metadata.get(key):
                    self.fail(f"Knowledge metadata {key} missing: {relative}")
            policy_key = metadata.get("policy_key")
            effective_date = metadata.get("effective_date")
            version = metadata.get("version")
            if effective_date:
                try:
                    date.fromisoformat(effective_date)
                except ValueError:
                    self.fail(f"Invalid effective_date in {relative}: {effective_date}")
            if version:
                try:
                    parsed_version = int(version)
                    if parsed_version < 1:
                        raise ValueError("version must be positive")
                except ValueError:
                    self.fail(f"Invalid version in {relative}: {version}")
                    parsed_version = None
            else:
                parsed_version = None
            if policy_key and effective_date and parsed_version is not None:
                key = (effective_date, parsed_version)
                if key in versions[policy_key]:
                    self.fail(f"Duplicate knowledge policy version: {policy_key} {key}")
                versions[policy_key].add(key)
                topic_counts[policy_key] += 1
                is_current = metadata.get("is_current")
                if is_current not in {"true", "false"}:
                    self.fail(f"Knowledge metadata is_current must be true or false: {relative}")
                elif is_current == "true":
                    current_versions[policy_key].append(key)
            paragraph_ids = PARAGRAPH_ID.findall(parts[2])
            if not paragraph_ids:
                self.fail(f"Knowledge document has no paragraph IDs: {relative}")
            elif len(paragraph_ids) != len(set(paragraph_ids)):
                self.fail(f"Knowledge document repeats paragraph IDs: {relative}")
        conflict_count = sum(count > 1 for count in topic_counts.values())
        expected_conflicts = manifest.get("policy_conflict_count")
        if isinstance(expected_conflicts, bool) or not isinstance(expected_conflicts, int):
            self.fail("Manifest policy_conflict_count must be an integer")
        elif conflict_count != expected_conflicts:
            self.fail(f"Policy conflict count mismatch: {conflict_count} != {expected_conflicts}")
        for policy_key, available in versions.items():
            if current_versions[policy_key] != [max(available)]:
                self.fail(f"Current knowledge version is not the latest: {policy_key}")
        expected_versions = manifest.get("policy_versions")
        if isinstance(expected_versions, dict):
            observed_versions = {key: sorted(version for _, version in values) for key, values in versions.items()}
            if observed_versions != expected_versions:
                self.fail("Knowledge policy_versions differ from manifest")

    def run(self, manifest_path: Path) -> bool:
        manifest = self.load_json(manifest_path)
        if manifest is None:
            return False
        self.check_metadata(manifest)
        self.check_files(manifest)
        self.check_orders(manifest)
        self.check_knowledge(manifest)
        return not self.errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, default=Path("data_manifest.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    verification = Verification(root)
    if verification.run(manifest_path):
        print(f"Data manifest verified: {manifest_path}")
        return 0
    for error in verification.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"Data manifest verification failed ({len(verification.errors)} error(s))", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
