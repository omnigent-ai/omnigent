# Dependabot auto-merge

Ordinary stable dependency patches and minors can merge without maintainer approval
when their diff, registry metadata, security reports and CI satisfy
[`dependabot-automerge.json`](dependabot-automerge.json). Explicit exceptions
remain normal Dependabot PRs for review. Individual PRs remain the default;
only the cross-pinned MLflow packages are grouped.

The Actions variable `DEPENDABOT_AUTOMERGE_MODE` selects `report` (read-only,
also the default when unset) or `merge`. The worker runs every 30 minutes using
scripts and policy from `main`, reading PR files as data. It never executes PR
code. Merge mode uses a repository-scoped GitHub App token, with no workflow-token
fallback or branch-protection bypass.

## Scope and exceptions

| Category | Automatic scope | Required compatibility evidence |
| --- | --- | --- |
| Python | Stable patches/minors, including existing manifest floor increases and corresponding transitive graph updates | Full selected Python matrix, database/Windows CI, integration and full E2E/UI workflows; PyPI artifact integrity and age |
| Web | Stable patches/minors in the web workspace, including necessary transitive graph updates | Frontend tests/types/build, browser contract and full browser E2E, production image build; npm integrity and age |
| GitHub Actions | Same-identity SHA-pin upgrades to stable minor/patch releases, exercised by PR CI | Verified upstream release pins, mapped consuming workflow and required compatibility workflows |
| Rust | Stable patches/minors in either Codex parity sidecar or omnidev, including corresponding manifest/graph updates | Actual tests for the affected workspace; sidecar updates also require browser E2E |
| Pre-commit | Stable patches/minors of `pre-commit-hooks` | Unchanged hook settings and successful Lint |

Every changed version, including transitives, must qualify. Majors, downgrades,
prereleases and `0.x` minor changes require review. Named development tools
(currently Ruff and Pyrefly) may receive `0.x` minors. There is no prerelease
exception: the October 21 backlog audit found no prerelease targets.

The policy's `manual` patterns cover harness SDKs/CLIs, native/desktop/mobile
packages, publishing/release/token Actions, and compatibility-coupled PDF
packages. Changes to existing exact pins, compatibility overrides, dependency
sources, manifest scripts, Python extras/markers, workflow permissions or shell
commands remain manual. New or removed direct dependencies remain manual;
normal additions/removals of registry-backed transitive dependencies are allowed.
Unsupported or ambiguous version resolutions also require review.

Web updates shared with another workspace consumer remain manual because the
web suite does not establish that consumer's compatibility. Docker images remain
manual: repository Trivy reports do not inventory the built image. Standalone
harness CLI pins retain their existing Dependabot exclusions.

## CI access and merge authorization

A verified `dependabot[bot]` PR with GitHub bot type and a same-repository head may
start compatibility CI while Security Scan is pending. The CI gate loads trusted
scripts from `main` without first executing a PR-supplied Action. Changed Actions
must pass same-identity SHA-pin and upstream-release validation before dependent
jobs execute. Major updates can therefore run CI while remaining manual to merge.
Forks, spoofed identities and incomplete or stale API inventories do not receive
this CI exception.

The separate Security Scan still runs and must pass before automatic merging.
Secret, exfiltration, workflow-misuse, sensitive-path and Semgrep checks retain
their role. Dependabot's full-lock pip-audit is deferred to changed-version
advisory evaluation, so baseline advisories do not prevent tests from running.
Other authors retain their existing scan behavior.

The worker requires successful Merge Ready, Maintainer Approval, Security Scan,
Lint, Trivy, each category's workflows and its actual test jobs on the assessed
head. Coverage scope check names distinguish full consumer suites from older
smoke-only runs. A green workflow containing skipped tests is insufficient.
Required aggregate jobs check every selected matrix leg. Failed or pending tests
remain blockers; entirely skipped E2E label runs do not hide the latest executed
run.

Python/npm/Cargo reports must inventory every changed or added version in the
correct lockfile. High, critical and unknown-severity findings on those versions
require review. Low/medium findings and unchanged baseline vulnerabilities may
remain. Actions and hooks use GitHub advisory data. A green Security Scan alone
does not establish the advisory policy; maintainers merging directly must inspect
the reports.

The worker checks release integrity and a seven-day cooldown. It rechecks the
head/base, labels, draft/open state and reviewer objections before merging the
assessed head. `hold-automerge`, `skip-security-scan` and changes-requested reviews
prevent unattended merges. Existing human approval remains independent of registry
availability.

Maintainers can still apply `skip-security-scan` for a manual merge. That waiver
does not make the PR eligible for the unattended worker.

## Existing PRs and rollout verification

Policy changes apply to existing PRs on the next sweep because the worker reads
`main`. Existing heads still need current coverage evidence. Rebase or recreate
those PRs after workflow changes; rerunning an old workflow retains its original
YAML and cannot manufacture the new coverage contract.

The worker refreshes stale failed approval checks for eligible candidates. It can
rerun workflows blocked only by failed Security Gates after the scan becomes
green. Recovery stops at three attempts and does not retry real test failures.
Report mode never refreshes or merges.

After landing, inspect the worker summary and observe a qualifying update through
CI access, scans, approval, automatic merge and post-merge workflows. Confirm
manual categories and failed tests remain blocked. Local simulated-service tests
do not establish hosted merge behavior or live provider compatibility.

Python CI installs OpenShell and Kubernetes alongside the existing `all` extra.
OpenShell's SDK-to-gRPC gateway scenario then runs instead of skipping for a
missing SDK. Kubernetes tests use a simulated client; other optional sandbox
SDKs still lack installed-SDK coverage. The broader Windows sweep remains
nonblocking; the Windows smoke/unit job is required for Python and Actions.

To stop merging, set `report` and cancel the worker. To hold one update, apply
`hold-automerge`. Revert an already-merged problematic update separately.

For a read-only preview with Python 3.12+ and PyYAML 6.0.3:

```sh
DEPENDABOT_AUTOMERGE_MODE=report python3 .github/scripts/dependabot_automerge.py \
  --repository omnigent-ai/omnigent
```

`--pr-number NUMBER` checks structural and registry eligibility only; it never
merges or certifies CI/advisories. `--check-action-pins` checks CI access only.

Run the policy, production CLI boundary and gate tests:

```sh
uv run --no-sync pytest tests/scripts/test_dependabot*.py \
  tests/scripts/test_dependency_ci.py tests/scripts/test_security_gate.py \
  .github/scripts/maintainer_approval_test.py --reruns 0
```
