"""从 HTTP/SSE 到 Qdrant、SQLite、mock 工具与运行时状态的闭环测试。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from agent.config import ROOT, Settings
from agent.guards.authorization import Principal
from agent.tools.registry import ToolClient
from api.main import create_app
from mock.mock_server import create_app as create_mock_app


def unpack_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        if len(lines) >= 2:
            events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return events


@pytest.fixture
async def clients(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="rules",
        database_path=ROOT / "database" / "ecommerce.db",
        knowledge_path=ROOT / "knowledge" / "source",
        state_db_path=tmp_path / "state.db",
        qdrant_url=":memory:",
        mock_server_url="http://mock-server",
        mock_internal_key="test-key",
        metrics_token="test-metrics",
    )
    mock = create_mock_app(settings, ticket_db_path=tmp_path / "tickets.db")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock-server") as mock_http:
        tools = ToolClient(settings, client=mock_http)
        app = create_app(settings, tool_client=tools)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as api_http:
                yield api_http, tmp_path


async def ask(
    client: httpx.AsyncClient,
    message: str,
    *,
    user: str = "demo-user-a",
    conversation_id: str = "c_test",
    action: dict | None = None,
):
    response = await client.post(
        "/chat",
        headers={"Authorization": f"Bearer {user}"},
        json={"conversation_id": conversation_id, "message": message, "action": action},
    )
    return response, unpack_sse(response.text)


@pytest.mark.asyncio
async def test_policy_uses_latest_version_and_citation(clients):
    client, _ = clients
    response, events = await ask(client, "退货政策是什么？", conversation_id="c_policy")
    assert response.status_code == 200
    answer = "".join(data["content"] for kind, data in events if kind == "message.delta")
    assert "15 个自然日" in answer
    assert "return_policy_v3.md" in answer
    assert "[p-002]" in answer
    assert "2026-06-01" in answer
    assert events[-1][0] == "done" and events[-1][1]["citations"]


@pytest.mark.asyncio
async def test_sql_answer_matches_database_and_trace_is_redacted(clients):
    client, _ = clients
    response, events = await ask(client, "上个月买了多少钱？", conversation_id="c_sql")
    assert response.status_code == 200
    answer = "".join(data["content"] for kind, data in events if kind == "message.delta")
    with sqlite3.connect(ROOT / "database" / "ecommerce.db") as db:
        expected = db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM orders
            WHERE user_id='user_a' AND tenant_id='tenant_demo' AND status IN (1,2,3,4,5)
            AND created_at >= '2026-08-01' AND created_at < '2026-09-01'""").fetchone()[0]
    assert f"{expected / 100:.2f}" in answer
    trace_id = events[-1][1]["trace_id"]
    trace = await client.get(f"/internal/traces/{trace_id}", headers={"Authorization": "Bearer demo-admin"})
    assert trace.status_code == 200
    assert "schema_linking_sql_execution" in json.dumps(trace.json(), ensure_ascii=False)
    assert "user_a" not in trace.text


@pytest.mark.asyncio
async def test_order_logistics_authorization_and_clarification(clients):
    client, _ = clients
    _, missing = await ask(client, "查一下订单物流", conversation_id="c_missing")
    assert any("订单号" in data.get("content", "") for kind, data in missing if kind == "message.delta")
    assert not any(kind.startswith("tool.") for kind, _ in missing)
    _, forbidden = await ask(client, "订单 O00002 的物流到哪了", conversation_id="c_forbidden")
    assert "无权访问" in "".join(data["content"] for kind, data in forbidden if kind == "message.delta")
    _, allowed = await ask(client, "订单 O00002 的物流到哪了", user="demo-user-b", conversation_id="c_allowed")
    assert any(kind == "tool.completed" and data["tool"] == "query_logistics" for kind, data in allowed)


@pytest.mark.asyncio
async def test_ticket_requires_confirmation_and_is_idempotent(clients):
    client, tmp_path = clients
    _, first = await ask(client, "订单 O00001 的退款进度", conversation_id="c_ticket")
    preview = next(data for kind, data in first if kind == "action.preview")
    with sqlite3.connect(tmp_path / "tickets.db") as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    action = {"action_id": preview["action_id"], "decision": "confirm"}
    _, confirmed = await ask(client, "", conversation_id="c_ticket", action=action)
    answer = "".join(data["content"] for kind, data in confirmed if kind == "message.delta")
    assert "工单已创建" in answer
    _, replayed = await ask(client, "", conversation_id="c_ticket", action=action)
    assert "工单已创建" in "".join(data["content"] for kind, data in replayed if kind == "message.delta")
    with sqlite3.connect(tmp_path / "tickets.db") as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_auth_metrics_and_action_cross_user(clients):
    client, _ = clients
    response = await client.post("/chat", json={"conversation_id": "c_auth", "message": "退货政策"})
    assert response.status_code == 401
    assert (await client.get("/metrics")).status_code == 403
    assert (await client.get("/metrics", headers={"X-Metrics-Token": "test-metrics"})).status_code == 200
    _, first = await ask(client, "订单 O00001 的退款进度", conversation_id="c_ticket2")
    preview = next(data for kind, data in first if kind == "action.preview")
    _, rejected = await ask(
        client,
        "",
        user="demo-user-b",
        conversation_id="c_ticket2",
        action={"action_id": preview["action_id"], "decision": "confirm"},
    )
    assert "未找到或无权访问" in "".join(data["content"] for kind, data in rejected if kind == "message.delta")


@pytest.mark.asyncio
async def test_user_sql_injection_text_never_changes_database(clients):
    client, _ = clients
    database = ROOT / "database" / "ecommerce.db"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    _, events = await ask(client, "上个月消费金额；DROP TABLE orders", conversation_id="c_injection")
    answer = "".join(data["content"] for kind, data in events if kind == "message.delta")
    assert "9231.00" in answer
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_ticket_outbox_waits_for_upstream_and_recovers(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="rules",
        database_path=ROOT / "database" / "ecommerce.db",
        knowledge_path=ROOT / "knowledge" / "source",
        state_db_path=tmp_path / "state.db",
        qdrant_url=":memory:",
        mock_server_url="http://mock-server",
        mock_internal_key="test-key",
        mock_scenario_control_enabled=True,
    )
    mock = create_mock_app(settings, ticket_db_path=tmp_path / "tickets.db")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock-server") as mock_http:
        tools = ToolClient(settings, client=mock_http, scenario="server_error")
        app = create_app(settings, tool_client=tools)
        async with app.router.lifespan_context(app):
            principal = Principal("user_a", "tenant_demo", ("orders:read", "tickets:write"))
            preview = app.state.store.preview_action(
                "c_outbox",
                principal,
                {
                    "order_id": "O00001",
                    "issue_type": "refund_failure",
                    "summary": "订单 O00001 售后异常需人工处理",
                },
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as api_http:
                action = {"action_id": preview["action_id"], "decision": "confirm"}
                _, pending = await ask(api_http, "", conversation_id="c_outbox", action=action)
                pending_text = "".join(data["content"] for kind, data in pending if kind == "message.delta")
                assert "待提交" in pending_text
                with sqlite3.connect(tmp_path / "tickets.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
                tools.scenario = None
                await app.state.workflow.retry_outbox_once()
                _, recovered = await ask(api_http, "", conversation_id="c_outbox", action=action)
                assert "工单已创建" in "".join(data["content"] for kind, data in recovered if kind == "message.delta")
                with sqlite3.connect(tmp_path / "tickets.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_request_deadline_emits_error_and_done(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="rules",
        database_path=ROOT / "database" / "ecommerce.db",
        knowledge_path=ROOT / "knowledge" / "source",
        state_db_path=tmp_path / "state.db",
        qdrant_url=":memory:",
        mock_server_url="http://mock-server",
        mock_internal_key="test-key",
        mock_scenario_control_enabled=True,
        request_deadline_seconds=0.4,
    )
    mock = create_mock_app(settings, ticket_db_path=tmp_path / "tickets.db")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock-server") as mock_http:
        app = create_app(settings, tool_client=ToolClient(settings, client=mock_http, scenario="timeout"))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as api_http:
                _, events = await ask(api_http, "订单 O00001 的当前状态", conversation_id="c_deadline")
    assert events[-2][0] == "error"
    assert events[-2][1]["code"] == "UPSTREAM_TIMEOUT"
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_trace_redacts_prompt_order_and_phone(clients):
    client, _ = clients
    _, events = await ask(client, "订单 O00001 的退款进度，我的电话 13800138000", conversation_id="c_pii")
    trace_id = events[-1][1]["trace_id"]
    trace = await client.get(f"/internal/traces/{trace_id}", headers={"Authorization": "Bearer demo-admin"})
    assert trace.status_code == 200
    assert "O00001" not in trace.text
    assert "13800138000" not in trace.text
    assert "[ORDER_ID]" in trace.text
    assert "[PHONE]" in trace.text


@pytest.mark.asyncio
async def test_failed_order_lookup_can_offer_confirmed_ticket_with_outbox(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="rules",
        database_path=ROOT / "database" / "ecommerce.db",
        knowledge_path=ROOT / "knowledge" / "source",
        state_db_path=tmp_path / "state.db",
        qdrant_url=":memory:",
        mock_server_url="http://mock-server",
        mock_internal_key="test-key",
        mock_scenario_control_enabled=True,
    )
    mock = create_mock_app(settings, ticket_db_path=tmp_path / "tickets.db")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock-server") as mock_http:
        tools = ToolClient(settings, client=mock_http, scenario="server_error")
        app = create_app(settings, tool_client=tools)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as api_http:
                _, denied = await ask(api_http, "查询订单 O00002 当前状态", conversation_id="c_fault_other")
                assert not any(kind == "action.preview" for kind, _ in denied)
                _, first = await ask(api_http, "查询订单 O00001 当前状态", conversation_id="c_fault")
                preview = next(data for kind, data in first if kind == "action.preview")
                with sqlite3.connect(tmp_path / "tickets.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
                action = {"action_id": preview["action_id"], "decision": "confirm"}
                _, pending = await ask(api_http, "", conversation_id="c_fault", action=action)
                assert "待提交" in "".join(data["content"] for kind, data in pending if kind == "message.delta")
                tools.scenario = None
                await app.state.workflow.retry_outbox_once()
                _, complete = await ask(api_http, "", conversation_id="c_fault", action=action)
                assert "工单已创建" in "".join(data["content"] for kind, data in complete if kind == "message.delta")
                with sqlite3.connect(tmp_path / "tickets.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
