# Dependabot auto-merge

Routine updates can merge without human approval when their diff, release age,
security reports and relevant CI satisfy the policy in
[`dependabot-automerge.json`](dependabot-automerge.json). Anything else remains
a normal Dependabot PR for maintainer review.

The Actions variable `DEPENDABOT_AUTOMERGE_MODE` controls operation:

| Value | Behavior |
| --- | --- |
| Unset or `report` | Read-only candidate reports; require maintainer approval. |
| `merge` | Eligible PRs pass Maintainer Approval and merge after checks. |

The workflow runs every 30 minutes and supports manual dispatch. Scripts and
policy come from `main`; PR files are read through the GitHub API as data.
The isolated parser environment installs PyYAML 6.0.3. No PR code is executed
by the evaluator. Merging uses a repository-scoped GitHub App token so normal
post-merge workflows can trigger; no workflow-token fallback is permitted. The
App token requests contents, pull requests, workflows and actions write access
for merges and bounded gate refreshes.

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

The JSON policy lists exact packages/patterns and tested Actions paths. Dependabot
opens individual version and security update PRs by default, so one update needing
review does not hold up unrelated updates. Only the cross-pinned `mlflow`,
`mlflow-skinny` and `mlflow-tracing` packages are grouped together, separately for
version and security updates. Every changed package must qualify for auto-merge,
including transitives; an individual PR can still require manual review.

Keep GitHub's repository/organization **Grouped security updates** setting disabled
so ecosystems without an explicit group also receive individual security PRs.
Schedules, PR limits, cooldowns and existing exclusions still apply.

Major updates, prereleases, `0.x` minor updates, compatibility override changes,
new/removed packages, dependency-graph changes and source changes require review.
An npm update may also prune lock entries unreachable from every workspace
importer; removing a reachable package still requires review.
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

Same-repository Dependabot updates omit the old full-lock pip-audit step in
both modes, including updates requiring manual review. This lets compatibility
CI and Trivy run despite unrelated baseline advisories. Other authors retain
the existing audit. Selected Actions-only SHA-pin updates pass the workflow
sensitive-path guard after release verification, including major releases and
releases still in cooldown; this grants CI access, not merge eligibility.
Secret, exfiltration, workflow-misuse and Semgrep detectors still run. Arbitrary
workflow changes and other sensitive paths keep their existing guards.

The worker requires successful Security Scan, CI, Lint and Trivy on the exact
head, plus Merge Ready, Maintainer Approval and each category's workflows and
named jobs. It inspects jobs in the selected workflow run, so a skipped suite
does not qualify. Entirely skipped E2E runs caused by unrelated label events
are ignored in favor of the latest executed run; pending or failed test runs
still block merging. Python, npm and Cargo updates require every changed version
in the corresponding Trivy lockfile inventory. Action and hook releases are checked
against GitHub's advisory API (`actions` and `pip` ecosystems respectively).
High, critical or unknown findings on changed versions require review; low and
medium findings may remain. Findings on unchanged packages do not block merging.

A green Security Scan alone does not certify the advisory policy: the merge
worker enforces it. Maintainers merging directly must inspect the relevant
reports. Switching to report mode stops worker writes but does not invalidate
existing approval checks; rerun Maintainer Approval if it should reflect the
new mode. The separation of CI access from merge eligibility applies in both modes.

The evaluator compares files against the PR merge base, matching GitHub's
changed-file list. Main's current tip is retained for the final race check.

Existing human approval is checked before registry eligibility, so an approved
PR does not depend on PyPI availability. The worker rechecks current head/base,
labels and reviews, then uses `--match-head-commit`, without an admin bypass or
a queued native auto-merge request. GitHub still enforces branch requirements.

## Enable, observe and stop

Set `DEPENDABOT_AUTOMERGE_MODE=merge` to enable merging. After changing workflow
definitions, let Dependabot rebase or recreate existing PRs so fresh PR events
load the new definitions. A rerun keeps the original workflow YAML and may
retain the old scan behavior even though the scripts load from `main`. For eligible candidates in merge mode, the worker refreshes
failed Maintainer Approval checks after cooldown expiry, mode changes or hold
removal. Once Security Scan is green, it also reruns workflows whose only failed
jobs are Security Gate jobs. Both refresh paths stop at three total run attempts;
they do not retry genuine test failures. Report mode never refreshes or merges.

Observe the first eligible update in each ecosystem through scans, actual CI jobs, merge and post-merge workflows.
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
form never merges and does not check CI or vulnerability reports. Add
`--check-action-pins` as well to inspect the separate scan exception for action
pins; that result must not be used as a merge decision.

Run the focused tests with the repository development environment:

```sh
uv run --no-sync pytest tests/scripts/test_dependabot_automerge.py \
  tests/scripts/test_dependabot_config.py tests/scripts/test_security_gate.py \
  .github/scripts/maintainer_approval_test.py --reruns 0
```
