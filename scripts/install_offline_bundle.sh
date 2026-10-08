#!/usr/bin/env bash
set -euo pipefail

bundle_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$bundle_root"

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo "This bundle requires Linux x86_64" >&2
  exit 1
fi
for command in docker sha256sum tar curl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command not found: $command" >&2
    exit 1
  fi
done
docker compose version >/dev/null
sha256sum --check SHA256SUMS

mkdir -p project
tar -xzf source.tar.gz -C project
docker image load -i images.tar
cd project
bash scripts/init_qdrant_offline.sh
docker compose -f docker/docker-compose.offline.yml up -d --no-build --pull never
ready=false
for _ in $(seq 1 40); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${API_BIND_PORT:-8000}/health?readiness=true"; then
    ready=true
    break
  fi
  sleep 3
done
if [[ "$ready" != true ]]; then
  echo "API did not become ready within 120 seconds" >&2
  exit 1
fi
echo
echo "Offline MVP is ready on http://127.0.0.1:${API_BIND_PORT:-8000}"
