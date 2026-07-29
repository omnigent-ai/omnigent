# Sourced by evaluate-checks.sh. These checks gate every PR. e2e, e2e-ui, and
# integration are mock-LLM (no secrets) and run on ALL PRs -- same-repo and fork
# -- directly, like CI. They are in ALLOW_SKIP too because they are legitimately
# absent in some runs: draft PRs (empty matrix) and path-ignored PRs (the
# workflow doesn't run). The real-gateway e2e-ui tests run nightly only and are
# NOT PR checks, so they are not listed here.
#
# Keep in sync with live job names in .github/workflows/{ci,lint,docker-build,
# e2e,e2e-ui,integration,ui-snapshot,web-tests}.yml. CI enforces this via
# validate-required.py (the "Merge-ready required sync" check). Workflows
# intentionally left unscanned are listed in INTENTIONAL_UNSCANNED_WORKFLOWS
# in that script.

REQUIRED=(
  "DCO"
  "Pre-commit checks"
  "Mypy"
  "Merge-ready required sync"
  "Version lockstep check"
  "Docker build"
  "npm test"
  "Pytest (runtime-harnesses)"
  "Pytest (runtime-policies)"
  "Pytest (runtime-core)"
  "Pytest (inner-rest)"
  "Pytest (tools)"
  "Pytest (repl-sdk)"
  "Pytest (server-approvals)"
  "Pytest (server-integration)"
  "Pytest (server-rest)"
  "Pytest (spec-llms)"
  "Pytest (integration-mock)"
  "Pytest (runner-app)"
  "Pytest (stores)"
  "Pytest (stores-postgres)"
  "Pytest (stores-mysql)"
  "Pytest (misc)"
  "Pytest (databricks)"
  "Pytest (slack)"
  "Pytest (codex-parity)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  "UI Snapshot (visual baselines)"
  "Integration (openai-agents)"
)

ALLOW_SKIP=(
  "Docker build"
  "npm test"
  "Pytest (runtime-harnesses)"
  "Pytest (runtime-policies)"
  "Pytest (runtime-core)"
  "Pytest (inner-rest)"
  "Pytest (tools)"
  "Pytest (repl-sdk)"
  "Pytest (server-approvals)"
  "Pytest (server-integration)"
  "Pytest (server-rest)"
  "Pytest (spec-llms)"
  "Pytest (integration-mock)"
  "Pytest (runner-app)"
  "Pytest (stores)"
  "Pytest (stores-postgres)"
  "Pytest (stores-mysql)"
  "Pytest (misc)"
  "Pytest (databricks)"
  "Pytest (slack)"
  "Pytest (codex-parity)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  "UI Snapshot (visual baselines)"
  "Integration (openai-agents)"
)

is_allow_skip() { printf '%s\n' "${ALLOW_SKIP[@]}" | grep -qxF "$1"; }

# Maps an ALLOW_SKIP check to the workflow that produces it, so
# evaluate-checks.sh can tell a genuine skip (a CI Pytest shard path-skip, or a
# draft/path-ignored run) from a check that is merely absent because its
# workflow is still queued or re-running.
workflow_for() {
  case "$1" in
    "Docker build")          echo "Docker build" ;;
    "npm test")              echo "web Tests" ;;
    "Pytest ("*)             echo "CI" ;;
    "E2E Tests (shard "*)    echo "E2E Tests" ;;
    "E2E UI Tests (shard "*) echo "E2E UI Tests" ;;
    "UI Snapshot (visual baselines)") echo "UI Snapshot" ;;
    "Integration ("*)        echo "Integration Tests" ;;
    *)                       echo "" ;;
  esac
}
