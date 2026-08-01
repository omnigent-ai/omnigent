#!/bin/sh
# Source this (or let run-*.sh do it) to fully isolate this worktree's
# omnigent from the global ~/.omnigent install.
WORKTREE="$(cd "$(dirname "$0")" && pwd)"
export OMNIGENT_CONFIG_HOME="$WORKTREE/.omnigent-local"
export OMNIGENT_DATA_DIR="$WORKTREE/.omnigent-local/data"
export UV_DEFAULT_INDEX="https://pypi-proxy.cloud.databricks.com/simple"
export COREPACK_NPM_REGISTRY="https://npm-proxy.cloud.databricks.com/"
export OMNIGENT_LOG_TO_STDERR=1
export ROUTING_SERVER_PORT=6868
export ROUTING_FRONTEND_PORT=5273
