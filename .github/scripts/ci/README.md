# Dependency CI selection

The existing CI, E2E, E2E UI and Integration workflows use
`dependency_scope.py` to select consumers of isolated dependency changes.
This applies to both Dependabot PRs and maintainer bundles; author and labels
do not determine test coverage.

| Modified files | Tests retained in these workflows |
| --- | --- |
| `dev/omnidev/Cargo.lock` | Locked `omnidev` build and Rust tests |
| `tests/codex_parity/sidecar/Cargo.lock` | Locked sidecar build, Codex parity, two UI shards containing every sidecar fixture consumer |
| `pnpm-lock.yaml`, `web/package.json` | Full server E2E (including lockfile pin guards), browser-contract and UI E2E suites; web tests and other existing workflows still run |
| `.pre-commit-config.yaml` | Existing full pre-commit checks; no runtime tests |
| Any other file, added/deleted/renamed file, or unknown checkout | Full existing suites plus `omnidev` |

Bundles run the union of their consumers. Python dependencies, harness CLI
pins, workflow changes, and workspace configuration changes retain full CI.
Security scans, dependency cooldown, review eligibility and lint remain in
their existing workflows. This selects tests; it does not grant automerge.

Selection reads a depth-two checkout of the exact GitHub PR merge revision,
verifies its head parent against the event, and diffs its tree against the base
parent. It needs no API inventory or additional token permissions. Push,
scheduled and manually dispatched runs retain full coverage. Existing workflow
path filters still apply.

The UI filter uses pytest's resolved fixture closure, so tests depending on
`mocked_native_codex_session` indirectly are included. Filtering happens before
sharding. An empty selection or a skipped selected test fails. Keep new sidecar
consumers behind this fixture, or update the selector when adding another one.

`CI result` and `E2E UI result` always run after selection and the test jobs.
They require success from every selected consumer; an unexpected skip, missing
job, failure or cancellation fails the result. Intentional skips remain valid.
Merge Ready requires these results once their selection jobs exist, and never
accepts an explicitly skipped result. Older workflows without selection jobs
can finish their original checks. Check-run IDs order retries, so a queued
retry cannot be hidden by an older success with a completion timestamp.

Dependabot also requires successful consumer jobs and completed workflows.

To inspect the current selected browser cases without starting a server:

```sh
uv run --no-sync pytest tests/e2e_ui --collect-only \
  --ui-codex-sidecar-only -m 'not visual and not nightly' -q
```

To verify the selector and merge policy:

```sh
uv run --no-sync pytest tests/scripts/test_dependency_ci.py \
  tests/scripts/test_ci_results.py \
  tests/scripts/test_dependency_ui_selection.py \
  tests/scripts/test_dependabot_automerge.py --reruns 0 -q
```
