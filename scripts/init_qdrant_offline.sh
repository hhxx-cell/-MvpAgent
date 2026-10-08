#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

docker compose -f docker/docker-compose.offline.yml up -d --no-build --pull never qdrant
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${QDRANT_BIND_PORT:-6333}/healthz" >/dev/null; then
    exit 0
  fi
  sleep 2
done
echo "Qdrant did not become healthy within 60 seconds" >&2
exit 1
