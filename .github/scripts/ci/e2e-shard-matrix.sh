#!/usr/bin/env bash
# Emits the e2e shard matrix as `matrix=<json>` on $GITHUB_OUTPUT, or an EMPTY
# matrix ({"include":[]}) to skip. The consuming job must guard this value
# with a job-level `if:` before GitHub expands the matrix.
#
# Skips draft PRs and scopes without consumers in this suite.
#
# Env in:  EVENT_NAME, IS_DRAFT, NUM_SHARDS, RUN_TESTS (defaults to true).
# Shared by e2e.yml and e2e-ui.yml (differ in NUM_SHARDS).

set -euo pipefail

skip=false
if [[ "${IS_DRAFT:-false}" == "true" || "${RUN_TESTS:-true}" == "false" ]]; then
  skip=true
fi

if [[ "$skip" == "true" ]]; then
  echo 'matrix={"include":[]}' >> "$GITHUB_OUTPUT"
  echo "skip: empty matrix (event=$EVENT_NAME draft=${IS_DRAFT:-} run=${RUN_TESTS:-true})"
  exit 0
fi

inc=""
for ((i = 0; i < NUM_SHARDS; i++)); do
  inc+="{\"shard_id\":$i,\"num_shards\":$NUM_SHARDS},"
done
echo "matrix={\"include\":[${inc%,}]}" >> "$GITHUB_OUTPUT"
echo "run: $NUM_SHARDS shards (event=$EVENT_NAME)"
