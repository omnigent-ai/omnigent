#!/usr/bin/env bash
# Emits the integration-test harness matrix as `matrix=<json>` on $GITHUB_OUTPUT.
#
# Returns an EMPTY matrix ({"include":[]}) when the run should be skipped:
#   - draft PRs, or
#   - a fork's pull_request (no secrets there; forks run via the fork-e2e/**
#     mirror push instead).
# An empty matrix yields zero jobs and therefore NO check-runs. This is the
# whole reason for the indirection (mirrors e2e-shard-matrix.sh): a job-level
# `if:` skip of a matrixed job would instead leave one check-run with an
# unexpanded `Integration (${{ matrix.name }})` name.
#
# Single openai-agents leg: all tests now run against the mock LLM server.
# claude-sdk and codex reject "mock-model" as an unknown model (they validate
# against the Databricks model catalog even when mock_llm_base_url is set), so
# only openai-agents works without real credentials. The model name is unused
# in mock mode (model_name fixture returns "mock-model" regardless).
#
# Env in:  EVENT_NAME (github.event_name), IS_DRAFT, IS_FORK (both may be empty
#          on non-PR events).
# Out:     matrix={"include":[{"name":..,"harness":..,"model":..,"workers":..}, ...]}
#          (or {"include":[]} when skipped).

set -euo pipefail

skip=false
if [[ "${IS_DRAFT:-false}" == "true" ]]; then
  skip=true
fi
if [[ "$EVENT_NAME" == "pull_request" && "${IS_FORK:-false}" == "true" ]]; then
  skip=true
fi

if [[ "$skip" == "true" ]]; then
  echo 'matrix={"include":[]}' >> "$GITHUB_OUTPUT"
  echo "skip: empty matrix (event=$EVENT_NAME draft=${IS_DRAFT:-} fork=${IS_FORK:-})"
  exit 0
fi

read -r -d '' matrix <<'JSON' || true
{"include":[
{"name":"openai-agents","harness":"openai-agents","model":"databricks-gpt-5-4-mini","workers":4}
]}
JSON
# Collapse to one line so the GITHUB_OUTPUT key=value contract holds.
echo "matrix=$(echo "$matrix" | tr -d '\n ')" >> "$GITHUB_OUTPUT"
echo "run: integration harness matrix (event=$EVENT_NAME)"
