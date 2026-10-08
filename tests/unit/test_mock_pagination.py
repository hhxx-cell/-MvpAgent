"""分页与故障注入入口的对象级边界。"""

from pathlib import Path

from fastapi.testclient import TestClient

from agent.config import ROOT, Settings
from mock.mock_server import create_app


def test_pagination_only_returns_current_user_and_has_boundary(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_path=ROOT / "database" / "ecommerce.db",
        mock_internal_key="test-key",
        mock_scenario_control_enabled=False,
    )
    app = create_app(settings, ticket_db_path=tmp_path / "tickets.db")
    headers = {"X-Internal-Service-Key": "test-key", "X-Principal-ID": "user_a", "X-Tenant-ID": "tenant_demo"}
    with TestClient(app) as client:
        first = client.get("/orders", params={"page": 1, "page_size": 10}, headers=headers)
        assert first.status_code == 200
        body = first.json()
        assert len(body["items"]) == 10
        assert body["next_page"] == 2
        pages = (body["total"] + 9) // 10
        last = client.get("/orders", params={"page": pages, "page_size": 10}, headers=headers)
        assert last.status_code == 200
        assert last.json()["next_page"] is None
        all_ids = [item["order_id"] for item in body["items"] + last.json()["items"]]
        assert "O00002" not in all_ids
        assert client.get("/orders/O00002", headers=headers).status_code == 404
        assert client.get("/orders", headers={**headers, "X-Mock-Scenario": "server_error"}).status_code == 403
