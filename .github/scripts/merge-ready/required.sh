# Sourced by evaluate-checks.sh. These checks gate every PR. e2e, e2e-ui, and
# integration are mock-LLM (no secrets) and run on ALL PRs -- same-repo and fork
# -- directly, like CI. They are in ALLOW_SKIP too because they are legitimately
# absent in some runs: draft PRs (empty matrix) and path-ignored PRs (the
# workflow doesn't run). The real-gateway e2e-ui tests run nightly only and are
# NOT PR checks, so they are not listed here.
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
  "Pytest (runner-app)"
  "Pytest (stores)"
  "Pytest (misc)"
  "Pytest (databricks)"
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
  "Pytest (runner-app)"
  "Pytest (stores)"
  "Pytest (misc)"
  "Pytest (databricks)"
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

is_allow_skip() { printf '%s\n' "${ALLOW_SKIP[@]}" | grep -qxF "$1"; }

# Maps an ALLOW_SKIP check to the workflow that produces it, so
# evaluate-checks.sh can tell a genuine skip (a CI Pytest shard path-skip, or a
# draft/path-ignored run) from a check that is merely absent because its
# workflow is still queued or re-running.
workflow_for() {
  case "$1" in
    "Pytest ("*)             echo "CI" ;;
    "E2E Tests (shard "*)    echo "E2E Tests" ;;
    "E2E UI Tests (shard "*) echo "E2E UI Tests" ;;
    "Integration ("*)        echo "Integration Tests" ;;
    *)                       echo "" ;;
  esac
}
