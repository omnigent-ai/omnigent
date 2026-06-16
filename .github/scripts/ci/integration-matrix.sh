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
# One leg per wrapped harness, no pytest-shard splitting: the journey suite is
# a handful of tests per leg. The `Integration (...)` leg-name prefix is load-
# bearing -- nightly.yml's notify jq filter keys on it.
#
# Model pinning rationale:
# - claude-sdk on sonnet-4-6: tier 4, most TPM headroom.
# - codex on gpt-5-5: gpt-5-4-mini hit 429s historically; also halve its
#   workers (least rate-limit headroom; burn-in failures were codex-only,
#   clustered at peak PR traffic).
# - openai-agents on gpt-5-4-mini: green there historically.
# OMNIGENT_TEST_MODEL_SPREAD in the workflow may rebalance within the same
# provider/tier pool (tests/_model_pools.py).
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
{"name":"claude-sdk","harness":"claude-sdk","model":"databricks-claude-sonnet-4-6","workers":4},
{"name":"openai-agents","harness":"openai-agents","model":"databricks-gpt-5-4-mini","workers":4},
{"name":"codex","harness":"codex","model":"databricks-gpt-5-5","workers":2}
]}
JSON
# Collapse to one line so the GITHUB_OUTPUT key=value contract holds.
echo "matrix=$(echo "$matrix" | tr -d '\n ')" >> "$GITHUB_OUTPUT"
echo "run: integration harness matrix (event=$EVENT_NAME)"
