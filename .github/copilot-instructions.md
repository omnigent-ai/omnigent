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

A pull request that changes behaviour under `ap-web/` should add or update a
**colocated Vitest unit test** — a `*.test.ts` or `*.test.tsx` file beside the
component or module it touches. If a behaviour change ships without one, flag it.

- A change to user-facing UI behaviour additionally needs a Playwright test
  under `tests/e2e_ui/`. That requirement is already enforced by the
  `E2E UI Required` status check, so do not re-flag it here — focus the review
  on the colocated unit test.
- Do not ask for a test for styling/formatting-only changes, copy tweaks with
  no flow change, type-only changes, dependency bumps, or refactors with no
  observable behaviour change.
- A trivial, empty, or unrelated test does not count as coverage.
