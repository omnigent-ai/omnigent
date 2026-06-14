#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"
prepare_backend
ensure_node_toolchain

log "running pre-commit"
uv run pre-commit run --all-files --show-diff-on-failure

log "installing ap-web dependencies"
npm --prefix ap-web ci --legacy-peer-deps

log "type-checking ap-web"
npm --prefix ap-web run type-check
