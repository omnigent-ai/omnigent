#!/bin/sh
set -eu
. "$(cd "$(dirname "$0")" && pwd)/dev-env.sh"
# nvm's lazy-load shim breaks in non-interactive shells; use the binary directly.
PATH="$HOME/.nvm/versions/node/v24.14.0/bin:$PATH"
cd "$WORKTREE/web"
OMNIGENT_URL="http://localhost:$ROUTING_SERVER_PORT" exec pnpm run dev -- --port "$ROUTING_FRONTEND_PORT"
