#!/usr/bin/env bash
# Bring up the local Omnigent server + sbx sandbox, and register the
# sandbox as a host. Idempotent: safe to re-run after a reboot — reuses
# an already-running server and an already-provisioned sandbox instead
# of recreating them.
set -euo pipefail

REPO_ROOT="$HOME/workspace/omnigent"
KIT="$HOME/workspace/sbxkit/opencode"
SANDBOX_NAME="omnigent-sandbox"

# `omnigent server` is a canonical singleton keyed on a pidfile, not a
# fixed port: if one is already running (started by this script, the
# REPL, another tool, ...) it reuses whatever port THAT one bound,
# which need not be 6767. Discover the real port from the pidfile
# rather than assuming one.
DATA_DIR="${OMNIGENT_DATA_DIR:-$HOME/.omnigent}"
PID_FILE="$DATA_DIR/local_server.pid"

cd "$REPO_ROOT"

discover_port() {
  [ -f "$PID_FILE" ] || return 1
  local pid port
  pid=$(sed -n '1p' "$PID_FILE")
  port=$(sed -n '2p' "$PID_FILE")
  [ -n "$pid" ] && [ -n "$port" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 || return 1
  echo "$port"
}

PORT="$(discover_port || true)"
if [ -z "$PORT" ]; then
  echo "Starting omnigent server..."
  nohup uv run omnigent server >/tmp/omnigent-server.log 2>&1 &
  for _ in $(seq 1 30); do
    PORT="$(discover_port || true)"
    [ -n "$PORT" ] && break
    sleep 1
  done
  if [ -z "$PORT" ]; then
    echo "omnigent server did not come up; check /tmp/omnigent-server.log" >&2
    exit 1
  fi
  echo "omnigent server up on :${PORT}."
else
  echo "omnigent server already running on :${PORT}."
fi

SERVER_URL="http://host.docker.internal:${PORT}"
LOCAL_URL="http://localhost:${PORT}"

# One-time-per-port egress allow; `sbx policy allow` is idempotent so
# re-adding an existing rule on every run is harmless.
sbx policy allow network "localhost:${PORT},host.docker.internal:${PORT}" >/dev/null 2>&1 || true

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

# Open the browser from a detached background job (no TTY interaction,
# so no job-control conflict) rather than backgrounding `connect`
# itself — `connect` needs to be the terminal's actual foreground
# process, since it hands off to `sbx exec -it` for an interactive
# TTY session. Backgrounding it made it a background job w.r.t. the
# terminal, and its raw-mode terminal setup triggered SIGTTOU, which
# stalled it long enough to blow sbx's own exec-readiness deadline.
if command -v xdg-open >/dev/null 2>&1; then
  (sleep 2 && xdg-open "$LOCAL_URL" >/dev/null 2>&1) &
  disown
fi

echo "Connecting sandbox to server (Ctrl-C to disconnect)..."
uv run omnigent sandbox connect --provider sbx --sandbox-id "$SANDBOX_NAME" \
  --server "$SERVER_URL"
