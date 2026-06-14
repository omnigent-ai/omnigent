#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"
prepare_backend
ensure_node_toolchain
write_databricks_profile

ui_dir="$CRABBOX_ARTIFACT_DIR/e2e-ui"
mkdir -p "$ui_dir"

log "installing Playwright Chromium"
uv run playwright install --with-deps chromium

log "building ap-web SPA"
npm --prefix ap-web ci --legacy-peer-deps --no-audit --no-fund
npm --prefix ap-web run build

extra_args=()
if [ "${NIGHTLY_FULL:-false}" != "true" ]; then
  extra_args+=(-m "not nightly")
fi

export OPENAI_API_KEY=$LLM_API_KEY
export OPENAI_BASE_URL=$GATEWAY_BASE_URL

log "running UI e2e suite"
uv run pytest tests/e2e_ui \
  -v --tb=long --showlocals --log-level=INFO -r a \
  --ui-skip-build \
  --tracing=retain-on-failure \
  --screenshot=only-on-failure \
  --video=retain-on-failure \
  --junitxml="$ui_dir/junit.xml" \
  "${extra_args[@]}"
