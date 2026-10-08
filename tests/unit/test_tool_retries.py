"""真实 mock 故障响应下的有界重试与超时。"""

import time
from pathlib import Path

import httpx
import pytest

from agent.config import ROOT, Settings
from agent.guards.authorization import Principal
from agent.observability.metrics import TOOL_RETRIES
from agent.tools.registry import ToolClient, ToolError
from mock.mock_server import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [("rate_limit", "UPSTREAM_RATE_LIMIT"), ("server_error", "UPSTREAM_ERROR"), ("timeout", "UPSTREAM_TIMEOUT")],
)
async def test_mock_failures_are_bounded(tmp_path: Path, scenario: str, expected_code: str):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_path=ROOT / "database" / "ecommerce.db",
        mock_internal_key="test-key",
        mock_server_url="http://mock-server",
        mock_scenario_control_enabled=True,
        request_deadline_seconds=2.2,
        tool_max_attempts=2,
    )
    mock = create_app(settings, ticket_db_path=tmp_path / "tickets.db")
    principal = Principal("user_a", "tenant_demo", ("orders:read", "tickets:write"))
    before = TOOL_RETRIES.labels("query_order", expected_code)._value.get()
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock-server") as http:
        tools = ToolClient(settings, client=http, scenario=scenario)
        with pytest.raises(ToolError) as exc:
            await tools.query_order("O00001", principal)
    assert exc.value.code == expected_code
    assert time.monotonic() - started < 4
    if scenario != "timeout":
        assert TOOL_RETRIES.labels("query_order", expected_code)._value.get() > before
