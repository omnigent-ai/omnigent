# Copilot Code Review Instructions

## E2E Test Requirement

Every pull request that introduces a new feature **must** include at least one
end-to-end (e2e) test covering the happy-path behaviour of that feature.

- E2E tests live under `tests/e2e/`.
- If a PR adds new user-facing functionality and does not add or update an e2e
  test, flag it as a required change.
- Bug-fix or refactor PRs that do not change observable behaviour are exempt.

## Backend Test Coverage

A pull request that changes behaviour under `omnigent/` should add or update a
test in the suite matching the area it touches. If a behaviour change ships
without a covering test, flag it and name the suite the test belongs in.

Prefer a fast, focused **unit test** in the area suite — that is what most
changes need. Only expect an `integration` or `e2e` test when the change
genuinely spans components or full-stack flows; do not push for a heavier test
where a unit test would suffice.

Most backend areas mirror their source directory under `tests/`:

| Area changed (`omnigent/…`) | Expected test suite (`tests/…`) |
| --- | --- |
| `server/` | `server/` |
| `runner/` | `runner/` |
| `runtime/` | `runtime/` |
| `tools/` | `tools/` |
| `inner/` | `inner/` |
| `llms/` | `llms/` |
| `db/` | `db/` (flag schema migrations especially) |
| `policies/` | `policies/` |
| `repl/` | `repl/` |
| `entities/` | `entities/` |
| `stores/` | `stores/` |
| `host/` | `host/` |
| `spec/` | `spec/` |

- A test under `tests/integration/` or `tests/e2e/` that exercises the change
  also satisfies the requirement — don't insist on the exact area suite.
- Do not ask for a test for pure refactors, renames, type-only changes,
  dependency bumps, comment/docstring/logging edits, or anything with no
  observable behaviour change.
- A trivial, empty, or unrelated test does not count as coverage.
- When in doubt about whether a change needs a test, raise it as a question
  rather than a required change.

## Frontend Test Coverage

A pull request that changes behaviour under `web/` should add or update a
**colocated Vitest unit test** — a `*.test.ts` or `*.test.tsx` file beside the
component or module it touches. If a behaviour change ships without one, flag it.

- A change to user-facing UI behaviour additionally needs a Playwright test
  under `tests/e2e_ui/`. That requirement is already enforced by the
  `E2E UI Required` status check, so do not re-flag it here — focus the review
  on the colocated unit test.
- A UI / frontend PR should also include a **video or images** in the `Demo`
  section of the PR description (with the "UI / frontend change" box checked).
  If a UI PR has an empty Demo section, flag it as a request for a screenshot
  or recording.
- Do not ask for a test for styling/formatting-only changes, copy tweaks with
  no flow change, type-only changes, dependency bumps, or refactors with no
  observable behaviour change.
- A trivial, empty, or unrelated test does not count as coverage.

## Session Status and Liveness

Apply this checklist when a pull request under `omnigent/runner/`,
`omnigent/terminals/`, `omnigent/native/` or `omnigent/harnesses/` reads,
stores or decides from session status (`running` / `waiting` / `idle` /
`failed`), turn liveness, or whether a pane or process is busy. The contract is
in `AGENTS.md` under "Session status and liveness".

- Flag a new dict, set or attribute that holds session status outside
  `SessionStatusBook` (`omnigent/runner/session_status.py`), and a status edge
  that is sent to the server but not recorded in the book.
- Flag a decision that keeps a pane, process or turn alive only because a
  recorded `running` says so. It also needs first-hand evidence (a live runner
  turn, pane output, a harness probe, a pending prompt) and must still reach a
  verdict when the status never changes again. The runner idle watchdog's
  native-turn hold is the one sanctioned exception, bounded as `AGENTS.md`
  describes; flag a change that lets a re-asserted status renew it or removes
  its bound.
- For a new cached value, ask which channels write it: runner events, server
  relays, watchers or pollers, interrupts, reconnects, teardown. A channel that
  never writes it leaves the value stale.
- Flag a new `# custom-lint: disable=session-status-single-source` whose reason
  does not say what the container holds.
- A new native harness must declare `pane_reap` and `status_owner` and pass
  `tests/runner/test_native_pane_reap_conformance.py` without new
  `_KNOWN_GAPS` entries.
- Expect the fix's test to send the edge through the production route (the
  runner's HTTP route, the real watcher or poller, the relay endpoint), not to
  write the cache directly.
