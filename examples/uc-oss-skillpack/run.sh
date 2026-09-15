#!/usr/bin/env bash
# Convenience wrapper for the Dockerized UC OSS skillpack POC.
#
#   ./run.sh up      start the UC OSS server and bootstrap catalog/schema/volume
#   ./run.sh down    stop and remove the server (keeps ./data)
#   ./run.sh clean   down + delete ./data (wipes all stored packs)
#   ./run.sh logs    tail the server logs
#   ./run.sh status  show whether the REST API is up
#
# POC / not for production.
set -euo pipefail

cd "$(dirname "$0")"

UC_OSS_URI="${UC_OSS_URI:-http://localhost:8080}"

# Prefer `docker compose` (v2); fall back to `docker-compose` (v1).
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

wait_for_server() {
  echo "waiting for UC OSS REST API at ${UC_OSS_URI} ..."
  for _ in $(seq 1 40); do
    if curl -fsS "${UC_OSS_URI}/api/2.1/unity-catalog/catalogs" >/dev/null 2>&1; then
      echo "server is up."
      return 0
    fi
    sleep 2
  done
  echo "server did not become ready in time" >&2
  return 1
}

case "${1:-}" in
  up)
    mkdir -p data
    "${DC[@]}" up -d
    wait_for_server
    ./bootstrap.sh
    ;;
  down)
    "${DC[@]}" down
    ;;
  clean)
    "${DC[@]}" down
    rm -rf data
    echo "removed ./data"
    ;;
  logs)
    "${DC[@]}" logs -f
    ;;
  status)
    if curl -fsS "${UC_OSS_URI}/api/2.1/unity-catalog/catalogs" >/dev/null 2>&1; then
      echo "UC OSS REST API is up at ${UC_OSS_URI}"
    else
      echo "UC OSS REST API is NOT reachable at ${UC_OSS_URI}"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 {up|down|clean|logs|status}" >&2
    exit 2
    ;;
esac
