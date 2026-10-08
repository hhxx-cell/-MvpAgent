"""独立于只读业务库的运行时状态、审计链和加密 Outbox。"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from agent.guards.authorization import Principal
from agent.guards.pii import redact


def utc_now() -> datetime:
    return datetime.now(UTC)


def packed(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ActionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ActionClaim:
    status: str
    action_id: str
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class TraceStore:
    def __init__(self, path: Path, fernet: Fernet):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = fernet
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _init(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL, created_at TEXT NOT NULL, body_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL, payload_cipher BLOB NOT NULL,
                    payload_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
                    status TEXT NOT NULL, result_cipher BLOB
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    pending_ticket_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE,
                    payload_cipher BLOB NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL, next_attempt_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
                    last_order_id TEXT, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status,next_attempt_at);
            """)

    def _encrypt(self, value: dict[str, Any]) -> bytes:
        return self.fernet.encrypt(packed(value).encode())

    def _decrypt(self, value: bytes) -> dict[str, Any]:
        try:
            return json.loads(self.fernet.decrypt(value))
        except (InvalidToken, ValueError) as exc:
            raise ActionError("INTERNAL_ERROR", "状态数据不可用") from exc

    def save_trace(self, trace_id: str, conversation_id: str, principal: Principal, body: dict[str, Any]) -> None:
        clean = redact(body)
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?)",
                (trace_id, conversation_id, principal.principal_id, utc_now().isoformat(), packed(clean)),
            )

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT body_json FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
        return json.loads(row["body_json"]) if row else None

    def remember_order(self, conversation_id: str, principal: Principal, order_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT principal_id FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
            if existing and existing["principal_id"] != principal.principal_id:
                raise ActionError("FORBIDDEN_RESOURCE", "会话不可访问")
            db.execute(
                """INSERT INTO conversations VALUES (?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET last_order_id=excluded.last_order_id,
                updated_at=excluded.updated_at""",
                (conversation_id, principal.principal_id, order_id, utc_now().isoformat()),
            )

    def last_order(self, conversation_id: str, principal: Principal) -> str | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT principal_id,last_order_id FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        if row and row["principal_id"] != principal.principal_id:
            raise ActionError("FORBIDDEN_RESOURCE", "会话不可访问")
        return row["last_order_id"] if row else None

    def preview_action(
        self, conversation_id: str, principal: Principal, payload: dict[str, Any], ttl_seconds: int = 300
    ) -> dict[str, str]:
        action_id = "act_" + uuid.uuid4().hex
        expires_at = (utc_now() + timedelta(seconds=ttl_seconds)).isoformat()
        digest = hashlib.sha256(packed(payload).encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    conversation_id,
                    principal.principal_id,
                    principal.tenant_id,
                    "create_ticket",
                    self._encrypt(payload),
                    digest,
                    expires_at,
                    "pending",
                    None,
                ),
            )
        return {"action_id": action_id, "expires_at": expires_at}

    def claim_action(self, action_id: str, conversation_id: str, principal: Principal, decision: str) -> ActionClaim:
        if decision not in {"confirm", "cancel"}:
            raise ActionError("MISSING_ARGUMENT", "操作决定无效")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
            if (
                row is None
                or row["principal_id"] != principal.principal_id
                or row["tenant_id"] != principal.tenant_id
                or row["conversation_id"] != conversation_id
            ):
                raise ActionError("FORBIDDEN_RESOURCE", "操作不存在或无权访问")
            if row["status"] in {"complete", "cancelled", "pending_outbox", "failed"}:
                result = self._decrypt(row["result_cipher"]) if row["result_cipher"] else None
                return ActionClaim("replay", action_id, result=result)
            if row["status"] == "processing":
                if datetime.fromisoformat(row["expires_at"]) >= utc_now():
                    return ActionClaim("in_progress", action_id)
                if decision != "confirm":
                    raise ActionError("FORBIDDEN_RESOURCE", "已确认操作不能取消")
                payload = self._decrypt(row["payload_cipher"])
                if hashlib.sha256(packed(payload).encode()).hexdigest() != row["payload_hash"]:
                    raise ActionError("INTERNAL_ERROR", "操作快照校验失败")
                lease = (utc_now() + timedelta(seconds=60)).isoformat()
                db.execute("UPDATE actions SET expires_at=? WHERE action_id=?", (lease, action_id))
                return ActionClaim("claimed", action_id, payload=payload)
            if row["status"] != "pending" or datetime.fromisoformat(row["expires_at"]) < utc_now():
                db.execute("UPDATE actions SET status='expired' WHERE action_id=?", (action_id,))
                raise ActionError("FORBIDDEN_RESOURCE", "操作已过期")
            payload = self._decrypt(row["payload_cipher"])
            if hashlib.sha256(packed(payload).encode()).hexdigest() != row["payload_hash"]:
                raise ActionError("INTERNAL_ERROR", "操作快照校验失败")
            if decision == "cancel":
                result = {"status": "cancelled", "message": "已取消创建工单"}
                db.execute(
                    "UPDATE actions SET status='cancelled',result_cipher=? WHERE action_id=?",
                    (self._encrypt(result), action_id),
                )
                return ActionClaim("cancelled", action_id, result=result)
            lease = (utc_now() + timedelta(seconds=60)).isoformat()
            db.execute("UPDATE actions SET status='processing',expires_at=? WHERE action_id=?", (lease, action_id))
            return ActionClaim("claimed", action_id, payload=payload)

    def finish_action(self, action_id: str, result: dict[str, Any], status: str = "complete") -> None:
        if status not in {"complete", "failed"}:
            raise ValueError("invalid action status")
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE actions SET status=?,result_cipher=? WHERE action_id=? AND status='processing'",
                (status, self._encrypt(result), action_id),
            )

    def enqueue_outbox(self, action_id: str, payload: dict[str, Any], last_error: str) -> dict[str, str]:
        pending_id = "pt_" + uuid.uuid4().hex
        result = {"status": "pending", "pending_ticket_id": pending_id, "message": "工单待提交，稍后自动重试"}
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,result_cipher FROM actions WHERE action_id=?", (action_id,)).fetchone()
            if not row or row["status"] != "processing":
                raise ActionError("TICKET_CREATION_FAILED", "无法保存待提交工单")
            db.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
                (
                    pending_id,
                    action_id,
                    self._encrypt(payload),
                    "pending",
                    0,
                    utc_now().isoformat(),
                    last_error[:120],
                ),
            )
            db.execute(
                "UPDATE actions SET status='pending_outbox',result_cipher=? WHERE action_id=?",
                (self._encrypt(result), action_id),
            )
        return result

    def due_outbox(self, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self._connect()) as db, db:
            rows = db.execute(
                """SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=?
                ORDER BY next_attempt_at LIMIT ?""",
                (utc_now().isoformat(), limit),
            ).fetchall()
        return [
            {
                "pending_ticket_id": row["pending_ticket_id"],
                "action_id": row["action_id"],
                "payload": self._decrypt(row["payload_cipher"]),
                "attempts": row["attempts"],
            }
            for row in rows
        ]

    def resolve_outbox(self, pending_id: str, action_id: str, result: dict[str, Any]) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE outbox SET status='complete' WHERE pending_ticket_id=? AND status='pending'", (pending_id,)
            )
            db.execute(
                "UPDATE actions SET status='complete',result_cipher=? WHERE action_id=?",
                (self._encrypt(result), action_id),
            )

    def fail_outbox(self, pending_id: str, action_id: str, error: str) -> None:
        result = {"status": "failed", "message": "工单未能创建，请联系人工客服"}
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE outbox SET status='failed',last_error=? WHERE pending_ticket_id=? AND status='pending'",
                (error[:120], pending_id),
            )
            db.execute(
                "UPDATE actions SET status='failed',result_cipher=? WHERE action_id=?",
                (self._encrypt(result), action_id),
            )

    def postpone_outbox(self, pending_id: str, attempts: int, error: str) -> None:
        delay = min(300, 2 ** min(attempts, 8))
        next_at = (utc_now() + timedelta(seconds=delay)).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                """UPDATE outbox SET attempts=?,next_attempt_at=?,last_error=?
                WHERE pending_ticket_id=? AND status='pending'""",
                (attempts, next_at, error[:120], pending_id),
            )
