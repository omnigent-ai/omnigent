#!/usr/bin/env bash

# Backend-only local smoke test.
#
# Validates the Python backend and local API server from THIS checkout without
# building the web UI, configuring provider credentials, creating sessions, or
# running agents. Useful for a quick server/API smoke check on your working
# copy or current `main`.
#
# Everything (toolchain, project venv, config, data, database, artifacts, logs,
# caches) lives under one disposable runtime directory that is removed on exit,
# so the run never touches your real ~/.omnigent, ~/.config / ~/Library, or
# package caches.
#
# Usage:
#   scripts/backend-smoke.sh              # uses port 18080
#   PORT=18090 scripts/backend-smoke.sh   # override the port if 18080 is busy
#
# Requires: bash, python3 (3.12+), git, curl, and network access to PyPI. No
# provider credentials needed. Works on Linux and macOS.

set -euo pipefail

PORT="${PORT:-18080}"
repo_root="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

runtime_root="$(mktemp -d "${TMPDIR:-/tmp}/omnigent_backend_XXXXXX")"
toolchain_venv="$runtime_root/toolchain_venv"
project_venv="$runtime_root/project_venv"
runtime_home="$runtime_root/home"
runtime_tmp="$runtime_root/tmp"
runtime_config="$runtime_root/config"
runtime_data="$runtime_root/data"
runtime_cache="$runtime_root/cache"
runtime_logs="$runtime_root/logs"
runtime_artifacts="$runtime_root/artifacts"
runtime_db="$runtime_data/omnigent/chat.db"
server_pid=""

cleanup() {
  if [ -n "$server_pid" ]; then
    kill -TERM "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$runtime_root"
}
trap cleanup EXIT

mkdir -p \
  "$runtime_home" "$runtime_tmp" \
  "$runtime_config/xdg" "$runtime_data/xdg" "$runtime_cache/xdg" \
  "$runtime_cache/pip" "$runtime_cache/uv" \
  "$runtime_logs" "$runtime_artifacts" \
  "$(dirname "$runtime_db")" \
  "$runtime_config/omnigent" "$runtime_data/omnigent"

# Isolated environment shared by every step. HOME is the primary isolation
# lever (it covers ~/.config on Linux and ~/Library on macOS); the explicit
# UV_/PIP_/OMNIGENT_ overrides pin the toolchain and app state regardless of
# OS. XDG_* are set so an XDG var already exported in the caller's shell can't
# redirect state back to the real home. OMNIGENT_SKIP_WEB_UI leaves the server
# in API-only mode (no web UI build); UV_PROJECT_ENVIRONMENT points uv at the
# throwaway project venv; UV_PYTHON_DOWNLOAD=never keeps uv from fetching
# interpreters.
env_vars=(
  "HOME=$runtime_home"
  "TMPDIR=$runtime_tmp"
  "XDG_CONFIG_HOME=$runtime_config/xdg"
  "XDG_DATA_HOME=$runtime_data/xdg"
  "XDG_CACHE_HOME=$runtime_cache/xdg"
  "PIP_CACHE_DIR=$runtime_cache/pip"
  "UV_CACHE_DIR=$runtime_cache/uv"
  "UV_PROJECT_ENVIRONMENT=$project_venv"
  "UV_PYTHON_DOWNLOAD=never"
  "OMNIGENT_CONFIG_HOME=$runtime_config/omnigent"
  "OMNIGENT_DATA_DIR=$runtime_data/omnigent"
  "OMNIGENT_DATABASE_URI=sqlite:///$runtime_db"
  "OMNIGENT_SKIP_WEB_UI=true"
)

echo "Runtime dir: $runtime_root"
echo "Checkout:    $repo_root"

echo "Installing uv into a throwaway toolchain venv..."
python3 -m venv "$toolchain_venv"
python3 -m venv "$project_venv"
env "${env_vars[@]}" "$toolchain_venv/bin/python" -m pip install --quiet "uv>=0.11.8"

echo "Syncing project dependencies (uv sync --frozen)..."
( cd "$repo_root" && env "${env_vars[@]}" "$toolchain_venv/bin/uv" sync --frozen )

echo "Starting backend server on 127.0.0.1:$PORT (API-only)..."
nohup env "${env_vars[@]}" \
  "$project_venv/bin/omnigent" server \
    --host 127.0.0.1 \
    --port "$PORT" \
    --database-uri "sqlite:///$runtime_db" \
    --artifact-location "$runtime_artifacts" \
  < /dev/null > "$runtime_logs/omnigent_server.log" 2>&1 &
server_pid="$!"

echo "Waiting for /health..."
ready=""
for _ in {1..60}; do
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
    "http://127.0.0.1:${PORT}/health" || true)"
  if [ "$code" = "200" ]; then ready=1; break; fi
  sleep 0.5
done
if [ -z "$ready" ]; then
  echo "ERROR: server did not become healthy in time; last log lines:" >&2
  tail -n 40 "$runtime_logs/omnigent_server.log" >&2 || true
  exit 1
fi

echo "Smoke-testing API endpoints (expect 200):"
status=0
for path in / /health /docs /v1/agents /v1/sessions; do
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://127.0.0.1:${PORT}${path}")"
  printf "  %-14s %s\n" "$path" "$code"
  [ "$code" = "200" ] || status=1
done

if [ "$status" -eq 0 ]; then
  echo "OK: all endpoints returned 200."
else
  echo "FAIL: one or more endpoints did not return 200." >&2
fi
exit "$status"
