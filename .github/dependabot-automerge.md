# Dependabot auto-merge

Routine updates can merge without human approval when their diff, release age,
security reports and relevant CI satisfy the policy in
[`dependabot-automerge.json`](dependabot-automerge.json). Anything else remains
a normal Dependabot PR for maintainer review.

The Actions variable `DEPENDABOT_AUTOMERGE_MODE` controls operation:

| Value | Behavior |
| --- | --- |
| Unset or `report` | Report candidates; retain normal approval and scan behavior. |
| `merge` | Eligible PRs pass Maintainer Approval and merge after checks. |

The workflow runs every 30 minutes and supports manual dispatch. Scripts and
policy come from `main`; PR files are read through the GitHub API as data.
The isolated parser environment installs PyYAML 6.0.3. No PR code is executed
by the evaluator. Merging uses a repository-scoped GitHub App token so normal
post-merge workflows can trigger; no workflow-token fallback is permitted.

## Scope

| Category | Automatic updates | Additional requirements |
| --- | --- | --- |
| Python test/lint tools | Named tools, patches and stable minor updates | Root `uv.lock` only; unit and integration/E2E workflows; PyPI artifact hashes, sizes, sources and age. |
| Ordinary Python libraries | Named utilities, patches | Same checks. Auth, database, serialization, sandbox and harness libraries remain manual. |
| Web test/lint tools and types | Named tools and type packages, patches and stable minors | `pnpm-lock.yaml` and optional `web/package.json`; frontend tests, type checking, browser tests and production image build; npm integrity and age. |
| Ordinary web libraries | Named libraries, patches | Same checks. Updates shared with untested workspace consumers remain manual. |
| GitHub Actions | Selected checkout/setup/cache actions, patches and stable minors | Only SHA-pin lines may change. Each new SHA must resolve to an aged stable upstream release and be exercised in a changed PR-tested workflow/composite action. |
| Pre-commit hooks | `pre-commit-hooks`, patches and stable minors | Only the release revision changes; hook commands/settings stay fixed; updated hooks must pass Lint. |
| Rust sidecar | Named crates.io dependencies, patches | Only `tests/codex_parity/sidecar/Cargo.lock`; registry checksum/age, actual Codex parity job and browser workflow. |

The JSON policy lists exact packages/patterns and tested Actions paths. Updates
are grouped separately from ordinary review-required updates in Dependabot's
configuration. Every changed package must qualify, including transitives.
A mixed group remains manual; maintainers can split it.

Major updates, prereleases, `0.x` minor updates, compatibility override changes,
new/removed packages, dependency-graph changes and source changes require review.
Python manifests, exact web pins, hook commands, workflow permissions and shell
commands cannot be changed through this exception. Rust permits dependency
version references to follow the validated crate upgrades, but no new edges.

Harnesses, native/desktop integrations, mobile/release tooling and `omnidev`
remain manual. Docker updates also remain manual: the existing repository Trivy
scan does not establish the built image's vulnerability inventory. Automating
images needs image-specific validation in addition to the build/CLI smoke.

Manual review does not disable PR creation. Existing Dependabot exclusions for
standalone harness CLI directories and JavaScript majors remain unchanged.

## Security and merge requirements

Every candidate must be an open, non-draft, same-repository Dependabot PR against
`main`. Releases must be at least seven days old. Changes-requested reviews,
`hold-automerge`, and `skip-security-scan` prevent unattended merging.

In merge mode, validated updates omit the old full-lock pip-audit step so
unrelated baseline advisories cannot block compatibility CI. A validated
Actions-only pin update also passes the workflow sensitive-path guard. Secret,
exfiltration, workflow-misuse and Semgrep detectors still run. Arbitrary
workflow changes and other sensitive paths keep their existing guards.

The worker requires successful Security Scan, CI, Lint and Trivy on the exact
head, plus each category's workflows and named jobs. Skipped named jobs do not
qualify. Python, npm and Cargo updates require every changed version in the
corresponding Trivy lockfile inventory. Action and hook releases are checked
against GitHub's advisory API (`actions` and `pip` ecosystems respectively).
High, critical or unknown findings on changed versions require review; low and
medium findings may remain. Findings on unchanged packages do not block merging.

A green Security Scan alone does not certify the advisory policy: the merge
worker enforces it. Maintainers merging directly must inspect the relevant
reports. Switching to report mode does not invalidate existing checks; rerun
Security Scan and Maintainer Approval to restore their normal behavior.

Existing human approval is checked before registry eligibility, so an approved
PR does not depend on PyPI availability. The worker rechecks current head/base,
labels and reviews, then uses `--match-head-commit`, without an admin bypass or
a queued native auto-merge request. GitHub still enforces branch requirements.

## Enable, observe and stop

After landing, set `DEPENDABOT_AUTOMERGE_MODE=merge`. Existing candidates checked
in report mode need Security Scan and Maintainer Approval rerun once. New or
updated candidates use the current mode. Observe the first eligible update in
each ecosystem through scans, actual CI jobs, merge and post-merge workflows.
Local tests use simulated services and do not establish hosted merge behavior.

To stop, set `report` and cancel any running Dependabot Auto-merge workflow.
To hold one PR, apply `hold-automerge`. Revert an already-merged problematic
upgrade separately; changing the mode does not undo a merge.

For a read-only preview using Python 3.12+ and PyYAML 6.0.3:

```sh
DEPENDABOT_AUTOMERGE_MODE=report python3 .github/scripts/dependabot_automerge.py \
  --repository omnigent-ai/omnigent
```

Add `--pr-number NUMBER` to inspect structural/registry eligibility only. That
form never merges and does not check CI or vulnerability reports.

Run the focused tests with the repository development environment:

```sh
uv run --no-sync pytest tests/scripts/test_dependabot_automerge.py \
  tests/scripts/test_dependabot_config.py tests/scripts/test_security_gate.py \
  .github/scripts/maintainer_approval_test.py --reruns 0
```
