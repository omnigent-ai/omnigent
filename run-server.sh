#!/bin/sh
set -eu
. "$(cd "$(dirname "$0")" && pwd)/dev-env.sh"
cd "$WORKTREE"
exec uv run --no-sync omni server -c "$OMNIGENT_CONFIG_HOME/config.yaml" --port "$ROUTING_SERVER_PORT"
