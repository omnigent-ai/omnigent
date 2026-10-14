#!/usr/bin/env bash
# Emits the e2e shard matrix as `matrix=<json>` on $GITHUB_OUTPUT, or an EMPTY
# matrix ({"include":[]}) to skip. Consumers must guard the empty result before
# GitHub expands their matrix strategy.
#
# Skips only draft PRs. These suites are mock-LLM (no secrets), so fork PRs run
# directly, like CI.
#
# Env in:  EVENT_NAME, IS_DRAFT, NUM_SHARDS.
# Shared by e2e.yml and e2e-ui.yml (differ in NUM_SHARDS).

set -euo pipefail

skip=false
if [[ "${IS_DRAFT:-false}" == "true" ]]; then
  skip=true
fi

if [[ "$skip" == "true" ]]; then
  echo 'matrix={"include":[]}' >> "$GITHUB_OUTPUT"
  echo "skip: empty matrix (event=$EVENT_NAME draft=${IS_DRAFT:-})"
  exit 0
fi

inc=""
for ((i = 0; i < NUM_SHARDS; i++)); do
  inc+="{\"shard_id\":$i,\"num_shards\":$NUM_SHARDS},"
done
echo "matrix={\"include\":[${inc%,}]}" >> "$GITHUB_OUTPUT"
echo "run: $NUM_SHARDS shards (event=$EVENT_NAME)"
