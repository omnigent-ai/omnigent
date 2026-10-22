# Sourced by evaluate-checks.sh. These checks gate every PR. e2e, e2e-ui, and
# integration are mock-LLM (no secrets) and run on ALL PRs -- same-repo and fork
# -- directly, like CI. They are in ALLOW_SKIP too because they are legitimately
# absent in some runs: draft PRs (empty matrix) and path-ignored PRs (the
# workflow doesn't run). The real-gateway e2e-ui tests run nightly only and are
# NOT PR checks, so they are not listed here.
# Generated file -- do not hand-edit; it is replaced wholesale on every sync.

REQUIRED=(
  "DCO"
  "Pre-commit checks"
  "Docker build"
  "Web scope"
  "Web coverage scope"
  "Web result"
  "Docker scope"
  "Docker result"
  "Build unsigned AAB"
  "CI scope"
  "CI coverage scope"
  "CI result"
  "UI scope"
  "UI coverage scope"
  "E2E UI result"
  "Rust tests (omnidev)"
  "Pytest (codex-parity)"
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
  "E2E Tests (shard 0/2)"
  "E2E Tests (shard 1/2)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/10)"
  "E2E UI Tests (shard 1/10)"
  "E2E UI Tests (shard 2/10)"
  "E2E UI Tests (shard 3/10)"
  "E2E UI Tests (shard 4/10)"
  "E2E UI Tests (shard 5/10)"
  "E2E UI Tests (shard 6/10)"
  "E2E UI Tests (shard 7/10)"
  "E2E UI Tests (shard 8/10)"
  "E2E UI Tests (shard 9/10)"
  "E2E UI Tests (shard 0/2)"
  "E2E UI Tests (shard 1/2)"
  "Browser Contract UI"
  "UI Snapshot (visual baselines)"
  "Integration (claude-sdk)"
  "Integration (openai-agents)"
  "Integration (codex)"
)

ALLOW_SKIP=(
  "Docker build"
  "Web scope"
  "Web coverage scope"
  "Web result"
  "Docker scope"
  "Docker result"
  "Build unsigned AAB"
  "CI scope"
  "CI coverage scope"
  "CI result"
  "UI scope"
  "UI coverage scope"
  "E2E UI result"
  "Rust tests (omnidev)"
  "Pytest (codex-parity)"
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
  "E2E Tests (shard 0/2)"
  "E2E Tests (shard 1/2)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/10)"
  "E2E UI Tests (shard 1/10)"
  "E2E UI Tests (shard 2/10)"
  "E2E UI Tests (shard 3/10)"
  "E2E UI Tests (shard 4/10)"
  "E2E UI Tests (shard 5/10)"
  "E2E UI Tests (shard 6/10)"
  "E2E UI Tests (shard 7/10)"
  "E2E UI Tests (shard 8/10)"
  "E2E UI Tests (shard 9/10)"
  "E2E UI Tests (shard 0/2)"
  "E2E UI Tests (shard 1/2)"
  "Browser Contract UI"
  "UI Snapshot (visual baselines)"
  "Integration (claude-sdk)"
  "Integration (openai-agents)"
  "Integration (codex)"
)

# No `printf | grep -q`: under pipefail, grep exiting at its match makes printf
# die of SIGPIPE on a later entry, and the pipeline reads as "not listed".
is_allow_skip() {
  local name
  for name in "${ALLOW_SKIP[@]}"; do
    [[ "$name" == "$1" ]] && return 0
  done
  return 1
}

# A result check may be absent on older workflows, but must run once its
# selection job is present. An explicit skipped result is never green.
scope_for_result() {
  case "$1" in
    "Web result") echo "Web scope,Web coverage scope" ;;
    "Docker result") echo "Docker scope" ;;
    "CI result") echo "CI scope,CI coverage scope" ;;
    "E2E UI result") echo "UI scope,UI coverage scope" ;;
    *) echo "" ;;
  esac
}

# Maps an ALLOW_SKIP check to the workflow that produces it, so
# evaluate-checks.sh can tell a genuine skip (a CI Pytest shard path-skip, or a
# draft/path-ignored run) from a check that is merely absent because its
# workflow is still queued or re-running.
workflow_for() {
  case "$1" in
    "Web scope"|"Web coverage scope"|"Web result") echo "web Tests" ;;
    "Build unsigned AAB") echo "Android Bundle" ;;
    "Docker scope"|"Docker result"|"Docker build")          echo "Docker build" ;;
    "CI scope"|"CI coverage scope"|"CI result"|"Rust tests (omnidev)") echo "CI" ;;
    "UI scope"|"UI coverage scope"|"E2E UI result") echo "E2E UI Tests" ;;
    "Pytest ("*)             echo "CI" ;;
    "E2E Tests (shard "*)    echo "E2E Tests" ;;
    "E2E UI Tests (shard "*) echo "E2E UI Tests" ;;
    "Browser Contract UI")     echo "E2E UI Tests" ;;
    "UI Snapshot (visual baselines)") echo "UI Snapshot" ;;
    "Integration ("*)        echo "Integration Tests" ;;
    *)                       echo "" ;;
  esac
}
