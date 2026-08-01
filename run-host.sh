#!/bin/sh
set -eu
. "$(cd "$(dirname "$0")" && pwd)/dev-env.sh"
cd "$WORKTREE"
exec uv run --no-sync omni host "http://localhost:$ROUTING_SERVER_PORT"
