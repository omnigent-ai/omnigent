# Sourced by evaluate-checks.sh. Only always-on checks are required (unit
# tests, lint, type-check, PR template). The e2e and integration suites run
# on schedule/dispatch only and are intentionally absent from REQUIRED and
# ALLOW_SKIP so missing checks do not block contributor PRs.
# Generated file -- do not hand-edit; it is replaced wholesale on every sync.

REQUIRED=(
  "PR Template"
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
)

is_allow_skip() { printf '%s\n' "${ALLOW_SKIP[@]}" | grep -qxF "$1"; }

# Maps an ALLOW_SKIP check to the workflow that produces it, so
# evaluate-checks.sh can tell a genuine path-skip from a check that is
# merely absent because its workflow is still queued or re-running. In
# the public variant the only skippable checks are the CI Pytest jobs;
# everything else echoes "" (unmapped).
workflow_for() {
  case "$1" in
    "Pytest ("*) echo "CI" ;;
    *)           echo "" ;;
  esac
}
