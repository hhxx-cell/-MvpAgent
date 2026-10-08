"""演示环境的固定合成身份适配器。"""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant_id: str
    scopes: tuple[str, ...]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


DEMO_TOKENS: dict[str, Principal] = {
    "demo-user-a": Principal("user_a", "tenant_demo", ("orders:read", "tickets:write")),
    "demo-user-b": Principal("user_b", "tenant_demo", ("orders:read", "tickets:write")),
    "demo-admin": Principal("admin", "tenant_demo", ("traces:read",)),
}


def authenticate(request: Request) -> Principal:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(401, detail={"code": "AUTHENTICATION_REQUIRED", "message": "请先登录"})
    principal = DEMO_TOKENS.get(value.removeprefix("Bearer ").strip())
    if principal is None:
        raise HTTPException(401, detail={"code": "AUTHENTICATION_REQUIRED", "message": "身份凭证无效"})
    return principal


def require_scope(principal: Principal, scope: str) -> None:
    if not principal.has(scope):
        raise HTTPException(403, detail={"code": "FORBIDDEN_RESOURCE", "message": "无权访问"})


def owned_order_exists(database_path: Path, principal: Principal, order_id: str) -> bool:
    """故障兜底只用受限历史快照验证归属；mock 建单时还会再验证。"""
    if not principal.has("orders:read") or not re.fullmatch(r"O\d{5,}", order_id):
        return False
    path = database_path.resolve()
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT 1 FROM orders WHERE order_id=? AND user_id=? AND tenant_id=? LIMIT 1",
                (order_id, principal.principal_id, principal.tenant_id),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
