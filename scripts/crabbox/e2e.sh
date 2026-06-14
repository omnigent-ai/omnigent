#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"
prepare_live_backend

e2e_dir="$CRABBOX_ARTIFACT_DIR/e2e"
mkdir -p "$e2e_dir/progress"

export E2E_TMP_BASE=${E2E_TMP_BASE:-"$e2e_dir/tmp"}
export PYTEST_PROGRESS_LOG_DIR=${PYTEST_PROGRESS_LOG_DIR:-"$e2e_dir/progress"}
export OMNIGENT_TOKEN_USAGE_JSON=${OMNIGENT_TOKEN_USAGE_JSON:-"$e2e_dir/tokens.json"}
export OMNIGENT_TEST_MODEL_SPREAD=${OMNIGENT_TEST_MODEL_SPREAD:-1}
export OMNIGENT_TEST_MODEL_POOL_GPT=${OMNIGENT_TEST_MODEL_POOL_GPT:-databricks-gpt-5-5,databricks-gpt-5-4-mini}

mkdir -p "$E2E_TMP_BASE"

extra_args=()
if [ "${FORCE_ALL_TESTS:-false}" = "true" ]; then
  extra_args+=(--no-skip-known)
fi
if [ "${NIGHTLY_FULL:-false}" != "true" ]; then
  extra_args+=(-m "not nightly")
fi

workers=${PYTEST_WORKERS:-2}

log "running live e2e suite with $workers workers"
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u DATABRICKS_TOKEN \
  uv run pytest tests/e2e/ \
    --llm-api-key "$LLM_API_KEY" \
    --profile default \
    --harness databricks \
    -n "$workers" \
    --dist=loadscope \
    --max-worker-restart=0 \
    --timeout="${PYTEST_TIMEOUT:-180}" \
    --timeout-method=thread \
    --basetemp="$E2E_TMP_BASE" \
    --junitxml="$e2e_dir/junit.xml" \
    -v --tb=long --showlocals --log-level=INFO -r a \
    "${extra_args[@]}"
