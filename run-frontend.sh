#!/bin/sh
set -eu
. "$(cd "$(dirname "$0")" && pwd)/dev-env.sh"
# nvm's lazy-load shim breaks in non-interactive shells, so resolve a real
# binary: PATH first, then the newest nvm install, plus homebrew for pnpm.
if ! command -v node >/dev/null 2>&1; then
  NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
  [ -n "$NODE_BIN" ] && PATH="$NODE_BIN:$PATH"
fi
PATH="$PATH:/opt/homebrew/bin"
command -v pnpm >/dev/null 2>&1 || { echo "pnpm not found; see LOCAL_SETUP.md" >&2; exit 1; }
cd "$WORKTREE/web"
OMNIGENT_URL="http://localhost:$ROUTING_SERVER_PORT" exec pnpm run dev -- --port "$ROUTING_FRONTEND_PORT"
