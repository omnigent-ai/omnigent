# Security Policy

To report a security vulnerability, use
[GitHub private security advisories](https://github.com/omnigent-ai/omnigent/security/advisories/new).

Please do not open a public issue for security problems, and do not include live
credentials, tokens, or customer data in any report.

## Contributor PR security gate

CI for untrusted PRs is held behind a deterministic **Security Gate** so that
untrusted code is not checked out, built, or run on our runners — and the
Actions cache is not touched — until the diff has been vetted. The gate is a
reusable workflow (`.github/workflows/security-gate.yml`) run as the first job
of every CI workflow (`ci`, `lint`, `e2e`, `e2e-ui`); the real jobs declare
`needs: gate`, so a failing gate skips them entirely.

By trust tier (GitHub `author_association`):

- **Trusted** (`OWNER` / `MEMBER` / `COLLABORATOR`) and all non-PR events
  (push, schedule, dispatch): the gate passes through instantly, no scan.
- **Returning contributor** (`CONTRIBUTOR`): the gate runs the scan; a clean
  result lets CI proceed automatically, a finding blocks all CI.
- **First-time contributor**: GitHub's native *“require approval to run fork
  pull request workflows”* repo setting already holds every workflow until a
  maintainer clicks **Approve and run**; after approval the gate's scan still
  applies.

The scan inspects the PR diff for committed secrets, changes to privileged repo
config (CI workflows, `.github/MAINTAINER`, `CODEOWNERS`, `.github/scripts`),
CI-workflow misuse (`pull_request_target` + PR-head checkout, unpinned actions),
and known code-execution / obfuscation patterns (semgrep, local ruleset). It
only *statically* analyses the diff and runs with **no secrets** on fork PRs,
and the scanner itself always runs from `main`, so a PR cannot weaken its own
gate.

This gate is **not** a merge-required check: it gates CI, not the merge button
directly. Merge stays blocked transitively (the skipped pytest/e2e checks are
required) and `Maintainer Approval` remains the ultimate gate.
