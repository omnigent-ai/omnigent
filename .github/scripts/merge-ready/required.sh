# Sourced by evaluate-checks.sh. The unit/lint/type-check checks gate every PR.
# The e2e + e2e-ui suites also gate PRs, but only run with secrets on same-repo
# PRs (maintainer branches); fork PRs cannot read the LLM_API_KEY /
# GATEWAY_BASE_URL secrets, so their e2e jobs skip via a workflow fork guard
# until a maintainer approves the PR (which triggers the fork-e2e mirror).
# The e2e and integration check names are in BOTH REQUIRED and ALLOW_SKIP so
# that path-filtered same-repo PRs (e.g. ap-web-only) can skip them. However,
# fork PRs must NOT skip e2e: FORK_NEVER_SKIP overrides ALLOW_SKIP when
# IS_FORK=true, so a fork PR's missing e2e checks block merge.
# Generated file -- do not hand-edit; it is replaced wholesale on every sync.

REQUIRED=(
  "Pre-commit checks"
  "Pytest (runtime-harnesses)"
  "Pytest (runtime-policies)"
  "Pytest (runtime-core)"
  "Pytest (inner-terminal)"
  "Pytest (inner-env)"
  "Pytest (inner-tracing)"
  "Pytest (inner-rest)"
  "Pytest (tools)"
  "Pytest (repl-sdk)"
  "Pytest (server-responses)"
  "Pytest (server-rest)"
  "Pytest (spec-llms)"
  "Pytest (misc)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  "Integration (claude-sdk)"
  "Integration (openai-agents)"
  "Integration (codex)"
)

ALLOW_SKIP=(
  "Pytest (runtime-harnesses)"
  "Pytest (runtime-policies)"
  "Pytest (runtime-core)"
  "Pytest (inner-terminal)"
  "Pytest (inner-env)"
  "Pytest (inner-tracing)"
  "Pytest (inner-rest)"
  "Pytest (tools)"
  "Pytest (repl-sdk)"
  "Pytest (server-responses)"
  "Pytest (server-rest)"
  "Pytest (spec-llms)"
  "Pytest (misc)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  "Integration (claude-sdk)"
  "Integration (openai-agents)"
  "Integration (codex)"
)

# Checks that must NOT be skipped on fork PRs. When IS_FORK=true,
# is_allow_skip returns false for these even though they're in ALLOW_SKIP.
# This ensures fork PRs can't merge with e2e/integration never having run.
FORK_NEVER_SKIP=(
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  "Integration (claude-sdk)"
  "Integration (openai-agents)"
  "Integration (codex)"
)

is_allow_skip() {
  # Fork PRs: never skip e2e/integration checks.
  if [[ "${IS_FORK:-false}" == "true" ]]; then
    if printf '%s\n' "${FORK_NEVER_SKIP[@]}" | grep -qxF "$1"; then
      return 1
    fi
  fi
  printf '%s\n' "${ALLOW_SKIP[@]}" | grep -qxF "$1";
}

# Maps an ALLOW_SKIP check to the workflow that produces it, so
# evaluate-checks.sh can tell a genuine skip (a CI Pytest shard path-skip, or
# the fork guard skipping an e2e job) from a check that is merely absent
# because its workflow is still queued or re-running.
workflow_for() {
  case "$1" in
    "Pytest ("*)             echo "CI" ;;
    "E2E Tests (shard "*)    echo "E2E Tests" ;;
    "E2E UI Tests (shard "*) echo "E2E UI Tests" ;;
    "Integration ("*)        echo "Integration Tests" ;;
    *)                       echo "" ;;
  esac
}
