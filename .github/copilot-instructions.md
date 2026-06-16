# Copilot Code Review Instructions

## E2E Test Requirement

Every pull request that introduces a new feature **must** include at least one
end-to-end (e2e) test covering the happy-path behaviour of that feature.

- E2E tests live under `tests/e2e/`.
- If a PR adds new user-facing functionality and does not add or update an e2e
  test, flag it as a required change.
- Bug-fix or refactor PRs that do not change observable behaviour are exempt.
