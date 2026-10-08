"""A rejected model SQL candidate can use the already trusted query template."""

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent.config import Settings
from agent.graph import AgentWorkflow
from agent.guards.authorization import Principal
from agent.model_gateway import ModelGateway
from agent.observability.metrics import SQL_TEMPLATE_FALLBACK
from agent.router import Route
from agent.sql.executor import SQLExecutionError
from agent.sql.schema_catalog import SchemaCatalog
from agent.sql.validator import SQLValidationError
from agent.state import AgentState


class InvalidSQLGateway(ModelGateway):
    def __init__(self) -> None:
        self.route_calls = 0
        self.sql_calls = 0

    async def classify(self, question: str, last_order_id: str | None) -> tuple[Route, int]:
        self.route_calls += 1
        return Route(("sql",), sql_kind="spend_total"), 13

    async def generate_sql(self, question: str, schema: str, template_sql: str) -> tuple[str, int]:
        self.sql_calls += 1
        return "DROP TABLE orders", 7


@pytest.fixture
def sql_workflow(tmp_path: Path) -> tuple[AgentWorkflow, Path, InvalidSQLGateway]:
    path = tmp_path / "ecommerce.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT,
                amount_cents INTEGER, status INTEGER, created_at TEXT
            );
            INSERT INTO orders VALUES
                ('O00001', 'alice', 'tenantA', 1299, 3, '2026-08-02'),
                ('O00002', 'bob', 'tenantA', 9999, 3, '2026-08-03'),
                ('O00003', 'alice', 'tenantB', 8888, 3, '2026-08-04'),
                ('O00004', 'alice', 'tenantA', 500, 0, '2026-08-05');
        """)
        connection.commit()
    finally:
        connection.close()
    catalog = SchemaCatalog.from_mapping(
        {"orders": ["order_id", "user_id", "tenant_id", "amount_cents", "status", "created_at"]},
        status_codes={
            "orders": {
                0: "待支付",
                1: "已支付",
                2: "已发货",
                3: "已完成",
                4: "退款处理中",
                5: "退款失败",
            }
        },
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_path=path,
        llm_provider="openai_compatible",
        data_reference_time="2026-09-24T00:00:00+08:00",
    )
    gateway = InvalidSQLGateway()
    store = Mock()
    store.last_order.return_value = None
    workflow = AgentWorkflow(settings, store, Mock(), catalog, Mock(), gateway)
    return workflow, path, gateway


def sql_state() -> AgentState:
    return {
        "conversation_id": "test-fallback",
        "message": "上个月消费金额是多少？",
        "principal": Principal("alice", "tenantA", ("orders:read",)),
        "spans": [],
        "llm_tokens": 0,
    }


@pytest.mark.asyncio
async def test_invalid_model_sql_falls_back_to_scoped_template(sql_workflow) -> None:
    workflow, database_path, gateway = sql_workflow
    before_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    before_fallbacks = SQL_TEMPLATE_FALLBACK._value.get()
    state = sql_state()

    state.update(await workflow._route(state))
    result = await workflow._sql(state)

    assert state["route"].intents == ("sql",)
    assert gateway.route_calls == gateway.sql_calls == 1
    assert result["sql_rows"] == [{"total_cents": 1299, "order_count": 1}]
    assert result["sql_attempts"] == 2
    assert result["spans"][-1]["template_fallback"] is True
    assert "DROP TABLE" not in result["sql_template"]
    assert "12.99" in workflow._format_sql({**state, **result})
    assert SQL_TEMPLATE_FALLBACK._value.get() == before_fallbacks + 1
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (SQLValidationError("FORBIDDEN_RESOURCE", "missing trusted identity"), "FORBIDDEN_RESOURCE"),
        (SQLExecutionError("SQL_TIMEOUT"), "SQL_EXECUTION_FAILED"),
    ],
)
async def test_authorization_failure_and_timeout_do_not_retry(sql_workflow, failure, error_code) -> None:
    workflow, _, gateway = sql_workflow
    state = sql_state()
    state.update(await workflow._route(state))

    with patch("agent.graph.run_sql_with_corrections", side_effect=failure) as run:
        result = await workflow._sql(state)

    assert gateway.sql_calls == 1
    assert run.call_count == 1
    assert result["error_code"] == error_code
