#!/usr/bin/env bash
# Run synthetic end-to-end checks against the Compose stack on this host.
# This creates one mock after-sales ticket and writes evidence under docs/evidence.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
evidence_dir=docs/evidence/compose-server
compose_file=${COMPOSE_FILE:-docker/docker-compose.yml}
api_url="http://127.0.0.1:${API_BIND_PORT:-8000}"
qdrant_url="http://127.0.0.1:${QDRANT_BIND_PORT:-6333}"
mkdir -p "$evidence_dir"
run_tag=$(date -u +%Y%m%dT%H%M%SZ)

docker compose -f "$compose_file" ps > "$evidence_dir/ps.txt"
curl -fsS --max-time 10 "$api_url/health?readiness=true" > "$evidence_dir/health.json"
curl -fsS --max-time 10 "$qdrant_url/healthz" > "$evidence_dir/qdrant-health.txt"
curl -fsS --max-time 10 -H 'X-Metrics-Token: demo-metrics-token' \
  "$api_url/metrics" > "$evidence_dir/metrics.txt"

chat() {
  local token=$1
  local conversation=$2
  local message=$3
  local output=$4
  local payload
  payload=$(python3 - "$conversation" "$message" <<'PY'
import json
import sys
print(json.dumps({"conversation_id": sys.argv[1], "message": sys.argv[2]}, ensure_ascii=False))
PY
)
  curl -fsS -N --max-time 30 -X POST "$api_url/chat" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    --data-binary "$payload" > "$output"
}

chat demo-user-a "smoke_policy_$run_tag" '退货政策期限是多少？' "$evidence_dir/policy.sse"
chat demo-user-a "smoke_sql_$run_tag" '我上个月消费金额是多少？' "$evidence_dir/sql.sse"
chat demo-user-a "smoke_order_$run_tag" '查询订单 O00001 当前状态' "$evidence_dir/order.sse"
chat demo-user-b "smoke_denied_$run_tag" '查询订单 O00001 当前状态' "$evidence_dir/denied.sse"
chat demo-user-a "smoke_clarify_$run_tag" '帮我看物流。' "$evidence_dir/clarify.sse"
chat demo-user-a "smoke_ticket_$run_tag" '为订单 O00001 创建工单' "$evidence_dir/ticket-preview.sse"

action_id=$(python3 - "$evidence_dir/ticket-preview.sse" <<'PY'
import json
import sys
for block in open(sys.argv[1], encoding="utf-8").read().split("\n\n"):
    if block.startswith("event: action.preview\n"):
        data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
        print(json.loads(data)["action_id"])
        break
else:
    raise SystemExit("missing action.preview")
PY
)
ticket_payload=$(python3 - "smoke_ticket_$run_tag" "$action_id" <<'PY'
import json
import sys
print(json.dumps({"conversation_id": sys.argv[1], "message": "", "action": {"action_id": sys.argv[2], "decision": "confirm"}}))
PY
)
for output in ticket-confirm.sse ticket-replay.sse; do
  curl -fsS -N --max-time 30 -X POST "$api_url/chat" \
    -H 'Authorization: Bearer demo-user-a' -H 'Content-Type: application/json' \
    --data-binary "$ticket_payload" > "$evidence_dir/$output"
done

trace_id=$(python3 - "$evidence_dir/ticket-confirm.sse" <<'PY'
import json
import sys
for block in open(sys.argv[1], encoding="utf-8").read().split("\n\n"):
    if block.startswith("event: done\n"):
        data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
        print(json.loads(data)["trace_id"])
        break
else:
    raise SystemExit("missing done event")
PY
)
curl -fsS --max-time 10 -H 'Authorization: Bearer demo-admin' \
  "$api_url/internal/traces/$trace_id" > "$evidence_dir/trace.json"

python3 - "$evidence_dir" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])

def events(filename):
    parsed = []
    for block in (root / filename).read_text(encoding="utf-8").split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("event: "):
            continue
        event = lines[0][7:]
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        parsed.append((event, data))
    assert parsed and parsed[-1][0] == "done", filename
    assert all(event != "error" for event, _ in parsed), filename
    return parsed

def answer(items):
    return "".join(data.get("content", "") for event, data in items if event == "message.delta")

policy = events("policy.sse")
assert "签收后 15 个自然日内" in answer(policy)
assert any(item.get("source_file") == "return_policy_v3.md" for item in policy[-1][1]["citations"])
sql = events("sql.sse")
assert "9231.00" in answer(sql)
order = events("order.sse")
assert "退款失败" in answer(order)
assert any(event == "tool.completed" for event, _ in order)
denied = events("denied.sse")
assert "未找到或无权访问" in answer(denied)
assert "658.00" not in answer(denied)
clarify = events("clarify.sse")
assert "请提供订单号" in answer(clarify)
assert all(event != "tool.started" for event, _ in clarify)
preview = events("ticket-preview.sse")
assert any(event == "action.preview" for event, _ in preview)
confirmed = events("ticket-confirm.sse")
replayed = events("ticket-replay.sse")
ticket_a = re.search(r"T[0-9A-F]{12}", answer(confirmed))
ticket_b = re.search(r"T[0-9A-F]{12}", answer(replayed))
assert ticket_a and ticket_b and ticket_a.group() == ticket_b.group()
trace = json.loads((root / "trace.json").read_text(encoding="utf-8"))
assert trace["trace_id"] == confirmed[-1][1]["trace_id"]
assert "O00001" not in json.dumps(trace, ensure_ascii=False)
health = json.loads((root / "health.json").read_text(encoding="utf-8"))
assert health["ready"] and all(value == "up" for value in health["components"].values())
summary = {
    "status": "passed",
    "checks": ["health", "qdrant", "metrics", "rag", "sql", "order", "authorization", "clarify", "ticket", "idempotency", "trace"],
    "ticket_id": ticket_a.group(),
    "trace_id": trace["trace_id"],
}
(root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
PY
