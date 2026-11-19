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
| Python | Stable patches/minors, including existing manifest floor and exact-pin increases and corresponding transitive graph updates | Full selected Python matrix, database/Windows CI, integration, full E2E/UI and production image build; PyPI artifact integrity and age |
| JavaScript | Stable patches/minors across mapped workspace consumers, including catalogs and simple override pins | Frontend tests/types/build, browser contract, full backend/browser E2E and production image build; executed consumer steps, npm integrity and age |
| GitHub Actions | Same-identity SHA-pin upgrades to stable minor/patch releases, exercised by PR CI | Verified upstream release pins, mapped occurrences (with named sharing exceptions) and required compatibility workflows |
| Rust | Stable patches/minors in either Codex parity sidecar or omnidev, including corresponding manifest/graph updates | Actual tests for the affected workspace; sidecar updates also require browser E2E |
| Pre-commit | Stable patches/minors of `pre-commit-hooks` | Unchanged hook settings and successful Lint |

Every changed version, including transitives, must qualify. Stable `0.x` minors
and ordinary exact-pin bumps qualify with the same required CI as other updates.
Majors, downgrades and prereleases require review, except the explicitly named
OpenTelemetry beta-to-beta updates in the established `0.NbM` streams listed
in `python.beta`. Replacing a stable resolution with a beta, or a beta with a stable
resolution, requires review. A newly added transitive from those named streams may
qualify. All still require cooldown, integrity, advisory and full Python CI evidence;
unlisted packages and other prerelease formats remain manual.

The policy's `manual` patterns cover harness SDKs/CLIs, native/desktop/mobile
runtime/packaging/updater packages, publishing CLIs (`@vscode/vsce*`, `wrangler`),
publishing/release/token/credential Actions, and compatibility-coupled PDF packages.
The deliberate KaTeX, Shiki, Vite and Tailwind compatibility pins also stay manual;
Shiki family updates need a coordinated maintainer review. Changes to requirement operators, upper bounds, complex compatibility overrides,
dependency sources, manifest scripts, Python extras/markers, workflow permissions
or shell commands remain manual. Existing catalog entries and simple override
pins may advance within the ordinary version policy. Other workspace settings
must remain unchanged. Simple overrides must agree with the lock and each
affected resolution; an unchanged compatible override is not a blocker.
Catalog and override requirements support exact, `^` and `~` stable versions only.
An unsupported resolved catalog requirement fails closed even when unchanged,
so it can block otherwise ordinary npm updates until the policy supports it.
Registry integrity, cooldown and advisory checks cover the changed resolved package
inventory; unused requirement floors do not create a package artifact to inspect.
New or removed direct dependencies remain manual;
normal additions/removals of registry-backed transitive dependencies are allowed.
A retained parallel version does not prevent upgrading one other resolution of
the same package. Adding a parallel version must advance the highest existing
version in its major line; adding an older or different-major line requires
review. Retained metadata and dependency edges remain validated. Multiple
simultaneous changed Python/Cargo versions, duplicate identities and other
ambiguous resolutions require review.

Mapped JavaScript consumers include web, Electron, VS Code, the extension SDK,
the example extension and Cloudflare. Shared graph changes require successful
workspace test/build steps or Cloudflare dry-run/image smoke steps in the job
metadata. An older green workflow without those steps is insufficient. Mappings
outside the required workflows are rejected. Electron has stubbed Node tests,
the SDK is type-checked and VS Code builds without `vsce package`; packaging,
updater signatures and live publishing remain untested, so their tools stay manual.
Unknown consumers and the separate `.github/ci-deps` CLI graph stay manual.
Docker images also stay manual because repository Trivy reports do not inventory
the built image. Standalone harness CLI pins retain their Dependabot exclusions.

## CI access and merge authorization

A verified `dependabot[bot]` PR with GitHub bot type and a same-repository head may
start compatibility CI while Security Scan is pending. The CI gate loads trusted
scripts from `main` without first executing a PR-supplied Action. Changed Actions
must pass same-identity SHA-pin and upstream-release validation before dependent
jobs execute. For unattended merging, every changed occurrence must be mapped to
required PR CI. Only the exact identities in `actions.shared_evidence` may share
an identical old/new SHA transition: `actions/checkout`, `actions/download-artifact`
and `docker/setup-buildx-action`. These helpers retrieve source/artifacts or set
up the builder; this is a deliberate compatibility exception, not evidence of
live publishing behavior. There are no wildcard sharing rules. Publishing Actions,
including `docker/build-push-action` even with `push: false`, stay manual and do
not receive the automatic CI exception.
The existing `skip-security-scan` waiver can also waive the Action-pin validation,
but always excludes unattended merging. Major updates can therefore run CI while
remaining manual to merge.
Forks, spoofed identities and incomplete or stale API inventories do not receive
this CI exception. This verifies the PR creator and repository, not commit authors
or signatures. A write collaborator can push to a Dependabot branch; the current
diff still has to pass the complete update policy before automatic approval.
Package install hooks and test code may execute before Security Scan finishes.

The separate Security Scan still runs and must pass before automatic merging.
Secret, exfiltration, workflow-misuse, sensitive-path and Semgrep checks retain
their role. Dependabot's full-lock pip-audit is deferred to changed-version
advisory evaluation, so baseline advisories do not prevent tests from running.
Other authors retain their existing scan behavior.

The worker requires successful Merge Ready, Maintainer Approval, Security Scan,
Lint, Trivy, each category's workflows and its actual test jobs on the assessed
head. Each required workflow must identify the assessed PR head and live `main`
ref in its recorded run metadata; a moved base requires fresh CI after rebasing.
The worker reads and rechecks that ref because a PR's `base.sha` can be stale.
Coverage scope check names distinguish full consumer suites from older
smoke-only runs. A green workflow containing skipped tests is insufficient.
Required aggregate jobs check every selected matrix leg. Failed or pending tests
remain blockers; entirely skipped E2E label runs do not hide the latest executed
run.

Python/npm/Cargo reports must inventory every changed or added version in the
correct lockfile. Targets under `dev/` use the separate `dev-tools` artifact and
paths relative to `dev/`; all other targets use the repository artifact.
High, critical and unknown-severity findings on those versions
require review. Low/medium findings and unchanged baseline vulnerabilities may
remain. Actions and hooks use GitHub advisory data. A green Security Scan alone
does not establish the advisory policy; maintainers merging directly must inspect
the reports.

The worker checks release integrity and a seven-day cooldown. It rechecks the
head/base, labels, draft/open state and reviewer objections before merging the
assessed head. `hold-automerge`, `skip-security-scan` and changes-requested reviews
prevent unattended merges. Existing human approval remains independent of registry
availability.

The automatic Dependabot exception in `Maintainer Approval` requires the same
CI and advisory evidence as the worker. Native GitHub auto-merge therefore cannot
use structural eligibility alone to satisfy that required check. Label changes
reevaluate approval so a hold also withdraws the automatic exception. Otto still
requires an actual maintainer review; no internal workflow change is needed.
The internal PR freshness updater explicitly excludes Dependabot. Keep branch
refreshes for unattended dependency candidates in this worker; reviewed manual
updates can continue through Otto's existing merge path.
These reads are not atomic with GitHub's merge operation, and a previously green
approval can outlive its assessed base in native auto-merge. Strict up-to-date
branch protection or a merge queue is needed for that platform-level guarantee;
this change does not alter repository settings.

Maintainers can still apply `skip-security-scan` for a manual merge. That waiver
does not make the PR eligible for the unattended worker.

## Lockfile regeneration

The existing `/regen` and scheduled consistency repair workflows preserve pnpm's
current lockfile instead of deleting it. Newly selected releases retain the
workspace cooldown; unchanged pins avoid unrelated upgrades and downgrades.
`/regen` uses uv 0.13.4, matching CI rather than the older pinned Action's default.
These are repair paths, not a replacement dependency generator. Dependabot can
still produce uv format/marker churn or incompatible transitive updates; those
diffs remain manual. The evaluator never executes PR code to rewrite them.

## Existing PRs and rollout verification

Policy changes apply to existing PRs on the next sweep because the worker reads
`main`. Existing heads still need current coverage evidence. Rebase or recreate
those PRs after workflow changes; rerunning an old workflow retains its original
YAML and cannot manufacture the new coverage contract.

The worker retries failed approval only after CI and advisories pass. It may rerun
CI blocked solely by failed Security Gates after Security Scan passes; real test
failures are never retried automatically. Gate recovery stops at three attempts.
If required workflows and consumer steps are green on one older base, and approval
is successful or recoverable, the worker can update the branch with an expected-head
guard. It stops that sweep and waits for new policy, scans and CI. The policy's
`max_base_refreshes` allows 20 branch refreshes per PR, counting merge commits
already in its history. Set it to `0` to disable automatic branch refreshes.
Existing PRs use this limit on the next sweep, including those stopped at the
previous limit of three. The summary reports the used and allowed counts when
the budget is exhausted. Incomplete inventories, mixed bases, holds, review
objections, conflicts and moved heads block recovery. API failures report bounded
context. Report mode never updates or merges.

Updates use the GitHub App identity, so subsequent events may receive ordinary
same-repository token/secrets treatment. Before updating, the worker inventories
workflow files at both the assessed PR head and current main. Every potentially
triggered `pull_request` synchronization or branch-push workflow, including optional
workflows and local reusable calls, must declare read-only permissions and have no
secret access or environment binding. Trivy retains only its existing SARIF upload
exception. Remote, missing or cyclic reusable calls and incomplete inventories
require manual refresh. Job conditions do not waive these checks.

Only literal branch/path filters and simple directory prefixes can prove a workflow
will not run; complex globs, negations and ignore filters are treated conservatively.
A merge push can contain main's changes too, so PR paths cannot exclude push workflows.
Ordinary package updates can still refresh. An Action update touching the current
release workflow cannot: its PR job requests OIDC and binds `test-pypi`, even though
its publish steps are skipped. Existing trusted-base preview/review workflows retain
their own gates; this worker changes neither their definitions nor repository settings.
Dependabot may stop rebasing after another identity updates its branch; conflicts
can need a maintainer's `@dependabot recreate`.
DCO skips merge commits; observe it and App-triggered CI on the first live refresh.

After landing, inspect the worker summary and observe a qualifying update through
CI access, scans, approval, automatic merge and post-merge workflows. Confirm
manual categories and failed tests remain blocked. Local simulated-service tests
do not establish hosted merge behavior or live provider compatibility.

Python CI installs OpenShell and Kubernetes alongside the existing `all` extra.
OpenShell's SDK-to-gRPC gateway scenario then runs instead of skipping for a
missing SDK. Kubernetes tests use a simulated client; other optional sandbox
SDKs still lack installed-SDK coverage. Full suites, the fixed E2E shard count,
SDK installation and the broader Windows sweep also apply to matching ordinary
PRs. Sidecar tests always execute, with compiled targets cached between runs.
Legacy dependency-smoke controls are deprecated for removal in 0.21.0. The broader
Windows sweep remains nonblocking; Windows smoke/unit is required for Python and
Actions.

The generated Merge Ready check list also names the coverage markers. Upstream
syncs must preserve those names; the worker independently enforces the markers.

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
