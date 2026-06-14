#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"
prepare_backend

mkdir -p "$CRABBOX_ARTIFACT_DIR/progress"

export PYTEST_PROGRESS_LOG_DIR=${PYTEST_PROGRESS_LOG_DIR:-"$CRABBOX_ARTIFACT_DIR/progress"}
export OMNIGENT_TOKEN_USAGE_JSON=${OMNIGENT_TOKEN_USAGE_JSON:-"$CRABBOX_ARTIFACT_DIR/tokens-pytest.json"}
export COVERAGE_FILE=${COVERAGE_FILE:-"$CRABBOX_ARTIFACT_DIR/.coverage.pytest"}
export COVERAGE_CORE=${COVERAGE_CORE:-sysmon}

workers=${PYTEST_WORKERS:-8}
dist=${PYTEST_DIST:-worksteal}

log "running non-live pytest suite with $workers workers"
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u DATABRICKS_TOKEN \
  uv run pytest tests \
    -n "$workers" \
    --dist="$dist" \
    --timeout="${PYTEST_TIMEOUT:-300}" \
    --junitxml="$CRABBOX_ARTIFACT_DIR/pytest.xml" \
    --cov=omnigent --cov-report=xml:"$CRABBOX_ARTIFACT_DIR/coverage.xml" \
    -v --tb=long --showlocals --log-level=INFO -r a
