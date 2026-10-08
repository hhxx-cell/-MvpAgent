"""确认进程中断后的租约恢复仍复用同一动作。"""

import sqlite3
from pathlib import Path

from agent.config import Settings
from agent.guards.authorization import Principal
from agent.observability.tracing import TraceStore


def test_stale_processing_can_be_reclaimed_with_same_payload(tmp_path: Path):
    settings = Settings(_env_file=None, app_env="test", state_db_path=tmp_path / "state.db")
    store = TraceStore(settings.resolved_state_path(), settings.fernet())
    principal = Principal("user_a", "tenant_demo", ("orders:read", "tickets:write"))
    payload = {"order_id": "O00001", "issue_type": "refund_failure", "summary": "合成工单"}
    preview = store.preview_action("c_reclaim", principal, payload)
    first = store.claim_action(preview["action_id"], "c_reclaim", principal, "confirm")
    assert first.status == "claimed"
    assert store.claim_action(preview["action_id"], "c_reclaim", principal, "confirm").status == "in_progress"
    with sqlite3.connect(settings.resolved_state_path()) as db:
        db.execute(
            "UPDATE actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE action_id=?", (preview["action_id"],)
        )
    recovered = store.claim_action(preview["action_id"], "c_reclaim", principal, "confirm")
    assert recovered.status == "claimed"
    assert recovered.payload == payload
