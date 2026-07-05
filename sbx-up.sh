#!/usr/bin/env bash
# Bring up the local Omnigent server + sbx sandbox, and register the
# sandbox as a host. Idempotent: safe to re-run after a reboot — reuses
# an already-running server and an already-provisioned sandbox instead
# of recreating them.
set -euo pipefail

REPO_ROOT="$HOME/workspace/omnigent"
KIT="$HOME/workspace/sbxkit/opencode"
SANDBOX_NAME="omnigent-sandbox"
PORT=6767

SERVER_URL="http://host.docker.internal:${PORT}"
LOCAL_URL="http://localhost:${PORT}"

cd "$REPO_ROOT"

if ! curl -fsS "$LOCAL_URL" >/dev/null 2>&1; then
  echo "Starting omnigent server on :${PORT}..."
  nohup uv run omnigent server >/tmp/omnigent-server.log 2>&1 &
  for _ in $(seq 1 30); do
    curl -fsS "$LOCAL_URL" >/dev/null 2>&1 && break
    sleep 1
  done
else
  echo "omnigent server already running on :${PORT}."
fi

SANDBOX_READY=false
if sbx ls -q 2>/dev/null | grep -qx "$SANDBOX_NAME"; then
  if sbx exec "$SANDBOX_NAME" bash -lc 'command -v omnigent' >/dev/null 2>&1; then
    SANDBOX_READY=true
    echo "sbx sandbox '${SANDBOX_NAME}' already exists and is provisioned, skipping create."
  else
    echo "sbx sandbox '${SANDBOX_NAME}' exists but omnigent isn't installed (likely a failed previous create) — recreating."
    sbx rm -f "$SANDBOX_NAME" >/dev/null 2>&1 || true
  fi
fi

if [ "$SANDBOX_READY" = false ]; then
  echo "Creating sbx sandbox '${SANDBOX_NAME}'..."
  uv run omnigent sandbox create --provider sbx --server "$SERVER_URL" \
    --kit "$KIT" --name "$SANDBOX_NAME"
fi

# `connect` attaches over an interactive TTY (`sbx exec -it`); starting
# that against a cold/stopped sandbox has raced sbx's own exec-readiness
# check ("context deadline exceeded"). Warm the sandbox first with a
# plain non-interactive exec so the VM/Docker daemon is already up.
sbx exec "$SANDBOX_NAME" true >/dev/null 2>&1 || true

echo "Connecting sandbox to server (Ctrl-C to disconnect)..."
uv run omnigent sandbox connect --provider sbx --sandbox-id "$SANDBOX_NAME" \
  --server "$SERVER_URL" &
CONNECT_PID=$!
trap 'kill -INT "$CONNECT_PID" 2>/dev/null || true' INT TERM

sleep 2
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$LOCAL_URL" >/dev/null 2>&1 &
fi

wait "$CONNECT_PID"
