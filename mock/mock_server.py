"""Synthetic order, logistics and ticket service for the MVP.

The business database is opened read-only. Ticket writes use a separate SQLite
file with an atomic idempotency constraint. This service is never a general SQL
proxy and never accepts a bearer token supplied by an end user.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent.config import ROOT, Settings

SHANGHAI = timezone(timedelta(hours=8))
ALLOWED_USERS = frozenset({"user_a", "user_b"})
TICKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    idempotency_key TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    ticket_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_action ON tickets(action_id);
"""


@dataclass(frozen=True)
class InternalIdentity:
    principal_id: str
    tenant_id: str


class TicketRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=40)
    issue_type: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    summary: str = Field(min_length=1, max_length=500)
    action_id: str = Field(min_length=8, max_length=120, pattern=r"^act_[A-Za-z0-9_-]+$")


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def error(status_code: int, code: str, message: str, *, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message}, headers=headers)


def owner_not_found() -> HTTPException:
    return error(404, "NOT_FOUND_OR_FORBIDDEN", "未找到或无权访问")


def business_connection(settings: Settings) -> sqlite3.Connection:
    path = settings.resolved_database_path()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ticket_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def init_tickets(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(ticket_connection(path)) as connection:
        connection.executescript(TICKET_SCHEMA)


def load_catalog() -> tuple[dict[int, str], dict[int, str]]:
    value = yaml.safe_load((ROOT / "database" / "schema_catalog.yaml").read_text(encoding="utf-8"))
    codes = value["status_codes"]
    order_codes = {int(code): str(label) for code, label in codes["orders"].items()}
    logistics_codes = {int(code): str(label) for code, label in codes["logistics"].items()}
    if set(order_codes) != set(range(8)) or set(logistics_codes) != set(range(4)):
        raise ValueError("状态字典不完整")
    return order_codes, logistics_codes


def load_scenarios() -> dict[str, dict[str, Any]]:
    value = yaml.safe_load((ROOT / "mock" / "scenarios.yaml").read_text(encoding="utf-8"))
    scenarios = value["scenarios"]
    if not isinstance(scenarios, dict) or not {
        "success",
        "rate_limit",
        "timeout",
        "server_error",
    }.issubset(scenarios):
        raise ValueError("mock 场景配置不完整")
    return scenarios


def public_order(row: sqlite3.Row, labels: dict[int, str]) -> dict[str, Any]:
    status = int(row["status"])
    return {
        "order_id": row["order_id"],
        "sku_id": row["sku_id"],
        "quantity": row["quantity"],
        "amount_cents": row["amount_cents"],
        "currency": row["currency"],
        "status": status,
        "status_label": labels[status],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def public_logistics(row: sqlite3.Row, labels: dict[int, str]) -> dict[str, Any]:
    status = int(row["status"])
    return {
        "logistics_id": row["logistics_id"],
        "order_id": row["order_id"],
        "carrier": row["carrier"],
        "tracking_no": row["tracking_no"],
        "status": status,
        "status_label": labels[status],
        "updated_at": row["updated_at"],
        "delivered_at": row["delivered_at"],
    }


def success(**values: Any) -> dict[str, Any]:
    return {"status": "success", "source": "mock_server", "observed_at": now_iso(), **values}


def require_internal_identity(request: Request) -> InternalIdentity:
    settings: Settings = request.app.state.settings
    supplied_key = request.headers.get("X-Internal-Service-Key", "")
    if not supplied_key or not hmac.compare_digest(supplied_key, settings.mock_internal_key):
        raise error(401, "AUTHENTICATION_REQUIRED", "内部服务凭证无效")
    principal_id = request.headers.get("X-Principal-ID", "")
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if principal_id not in ALLOWED_USERS or tenant_id != "tenant_demo":
        raise error(403, "FORBIDDEN_RESOURCE", "无权访问")
    return InternalIdentity(principal_id, tenant_id)


IdentityDep = Annotated[InternalIdentity, Depends(require_internal_identity)]


async def apply_scenario(request: Request, _: IdentityDep) -> None:
    name = request.headers.get("X-Mock-Scenario")
    if not name:
        return
    settings: Settings = request.app.state.settings
    if settings.app_env not in {"development", "test", "testing"} or not settings.mock_scenario_control_enabled:
        raise error(403, "MOCK_SCENARIO_DISABLED", "故障注入未启用")
    scenario = request.app.state.scenarios.get(name)
    if scenario is None:
        raise error(400, "UNKNOWN_MOCK_SCENARIO", "未知故障场景")
    delay = scenario.get("delay_seconds", 0)
    if delay:
        await asyncio.sleep(float(delay))
    status_code = scenario.get("status_code")
    if status_code == 429:
        retry_after = str(scenario.get("retry_after_seconds", 1))
        raise error(429, "UPSTREAM_RATE_LIMIT", "服务暂时繁忙", headers={"Retry-After": retry_after})
    if status_code and int(status_code) >= 500:
        raise error(int(status_code), "UPSTREAM_SERVER_ERROR", "服务暂时不可用")


ScenarioDep = Annotated[None, Depends(apply_scenario)]


def owned_order(connection: sqlite3.Connection, order_id: str, identity: InternalIdentity) -> sqlite3.Row:
    row = connection.execute(
        "SELECT order_id, sku_id, quantity, amount_cents, currency, status, created_at, updated_at "
        "FROM orders WHERE order_id = ? AND user_id = ? AND tenant_id = ?",
        (order_id, identity.principal_id, identity.tenant_id),
    ).fetchone()
    if row is None:
        raise owner_not_found()
    return row


def ticket_payload_hash(body: TicketRequest, identity: InternalIdentity) -> str:
    serialized = json.dumps(
        {
            **body.model_dump(),
            "principal_id": identity.principal_id,
            "tenant_id": identity.tenant_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_ticket_once(
    path: Path, body: TicketRequest, identity: InternalIdentity, idempotency_key: str
) -> tuple[str, bool]:
    digest = ticket_payload_hash(body, identity)
    ticket_id = "T" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12].upper()
    with closing(ticket_connection(path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_hash, ticket_id FROM tickets WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != digest:
                    raise error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他请求")
                connection.execute("COMMIT")
                return str(existing["ticket_id"]), True
            # A distinct key may not reuse the same confirmed action.
            reused_action = connection.execute(
                "SELECT idempotency_key FROM tickets WHERE action_id = ?", (body.action_id,)
            ).fetchone()
            if reused_action is not None:
                raise error(409, "ACTION_ALREADY_USED", "该操作已处理")
            connection.execute(
                "INSERT INTO tickets (idempotency_key, action_id, principal_id, tenant_id, "
                "order_id, payload_hash, ticket_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    body.action_id,
                    identity.principal_id,
                    identity.tenant_id,
                    body.order_id,
                    digest,
                    ticket_id,
                    now_iso(),
                ),
            )
            connection.execute("COMMIT")
            return ticket_id, False
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def create_app(settings: Settings | None = None, ticket_db_path: Path | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate_runtime()
    order_labels, logistics_labels = load_catalog()
    scenarios = load_scenarios()
    if ticket_db_path is None:
        ticket_db_path = Path(os.getenv("MOCK_TICKET_DB_PATH", ROOT / "runtime" / "mock_tickets.db"))
    if not ticket_db_path.is_absolute():
        ticket_db_path = ROOT / ticket_db_path
    init_tickets(ticket_db_path)

    app = FastAPI(title="合成售后 Mock 服务", version="1.0.0")
    app.state.settings = settings
    app.state.ticket_db_path = ticket_db_path
    app.state.scenarios = scenarios

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with closing(business_connection(settings)) as connection:
                connection.execute("SELECT 1 FROM orders LIMIT 1").fetchone()
            with closing(ticket_connection(ticket_db_path)) as connection:
                connection.execute("SELECT 1 FROM tickets LIMIT 1").fetchone()
        except sqlite3.Error:
            return JSONResponse({"status": "unavailable", "code": "DEPENDENCY_UNAVAILABLE"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok", "tickets": "ok"})

    @app.get("/orders")
    def list_orders(
        identity: IdentityDep,
        _: ScenarioDep,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        with closing(business_connection(settings)) as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM orders WHERE user_id = ? AND tenant_id = ?",
                (identity.principal_id, identity.tenant_id),
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT order_id, sku_id, quantity, amount_cents, currency, status, "
                "created_at, updated_at FROM orders WHERE user_id = ? AND tenant_id = ? "
                "ORDER BY created_at DESC, order_id DESC LIMIT ? OFFSET ?",
                (identity.principal_id, identity.tenant_id, page_size, (page - 1) * page_size),
            ).fetchall()
        next_page = page + 1 if page * page_size < total else None
        return success(
            items=[public_order(row, order_labels) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            next_page=next_page,
        )

    @app.get("/orders/{order_id}")
    def get_order(
        order_id: str,
        identity: IdentityDep,
        _: ScenarioDep,
    ) -> dict[str, Any]:
        with closing(business_connection(settings)) as connection:
            row = owned_order(connection, order_id, identity)
        return success(order=public_order(row, order_labels))

    @app.get("/orders/{order_id}/logistics")
    def get_logistics(
        order_id: str,
        identity: IdentityDep,
        _: ScenarioDep,
    ) -> dict[str, Any]:
        with closing(business_connection(settings)) as connection:
            owned_order(connection, order_id, identity)
            row = connection.execute(
                "SELECT logistics_id, order_id, carrier, tracking_no, "
                "status, updated_at, delivered_at "
                "FROM logistics WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise owner_not_found()
        return success(logistics=public_logistics(row, logistics_labels))

    @app.post("/tickets")
    def post_ticket(
        body: TicketRequest,
        response: Response,
        identity: IdentityDep,
        _: ScenarioDep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        if not idempotency_key or not 8 <= len(idempotency_key) <= 200:
            raise error(400, "IDEMPOTENCY_KEY_REQUIRED", "缺少有效的幂等键")
        with closing(business_connection(settings)) as connection:
            owned_order(connection, body.order_id, identity)
        try:
            ticket_id, replayed = create_ticket_once(ticket_db_path, body, identity, idempotency_key)
        except sqlite3.Error:
            raise error(503, "TICKET_STORE_UNAVAILABLE", "工单暂时无法提交") from None
        response.status_code = 200 if replayed else 201
        return {
            "status": "created",
            "source": "mock_server",
            "observed_at": now_iso(),
            "ticket_id": ticket_id,
            "idempotency_replayed": replayed,
        }

    return app


app = create_app()
