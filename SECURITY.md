# Security Policy

To report a security vulnerability, use
[GitHub private security advisories](https://github.com/omnigent-ai/omnigent/security/advisories/new).

Please do not open a public issue for security problems, and do not include live
credentials, tokens, or customer data in any report.

## Automated dependency CVE scanning

[Trivy CVE Scan](.github/workflows/trivy.yml) scans the repository on every pull
request, push to `main`, daily at 06:23 UTC, and manual workflow dispatch. The
scheduled scan catches newly published advisories even when dependencies have
not changed. It runs for trusted contributors too, after the existing PR
security gate permits CI to proceed.

Trivy statically scans supported dependency manifests and lockfiles, including
`uv.lock`, `pnpm-lock.yaml`, `Cargo.lock`, and `Gemfile.lock`, without installing
or executing project dependencies. Development dependencies, all severities,
and vulnerabilities without fixes are included. This is a dependency scan, not
a scan of built container images or a replacement for the contributor security
scan below. A separate `dev-tools` scan covers `dev/omnidev/Cargo.lock`, because
Trivy excludes root-level `dev/` directories from its filesystem traversal.

The initial rollout is **report-only**: vulnerabilities do not fail CI, but
scanner or report-generation errors do. A green check does not mean there are
no CVEs. Open the workflow's **Trivy CVE Scan** jobs to read the tables, or
download the **trivy-cve-report-repository** and **trivy-cve-report-dev-tools**
artifacts (retained for 30 days) for text, JSON, and SARIF reports. SARIF is
provided as an artifact, not uploaded to GitHub code scanning; this keeps the
workflow read-only and usable on fork PRs without additional security-product
configuration or credentials.

To reproduce locally with Trivy **v0.74.0**, from the repository root:

```bash
(
  set -e
  for target in . dev; do
    trivy fs --scanners vuln --include-dev-deps \
      --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL --ignore-unfixed=false \
      --skip-dirs '.git,**/.venv,**/node_modules' --timeout 10m --exit-code 0 "$target"
  done
)
```

Review findings by affected lockfile, installed version, and available fix;
prioritize reachable high/critical vulnerabilities. Any future blocking
threshold or vulnerability exception should be an explicit, reviewed policy
change rather than silently hiding the baseline.

## Contributor PR security gate

CI for untrusted PRs is held behind a deterministic security scan so that
untrusted code is not checked out, built, or run on our runners — and the
Actions cache is not touched — until the diff has been vetted. It is split into
two pieces so the scan work happens only **once per PR**:

- **`.github/workflows/security-scan.yml`** — runs the deterministic scan once
  on `pull_request` and produces the `Security Scan` check.
- **`.github/workflows/security-gate.yml`** — a reusable poller run as the first
  job (`gate`) of every CI workflow (`ci`, `lint`, `e2e`, `e2e-ui`, web
  tests); the real jobs declare `needs: gate`. It does not re-scan — for an
  untrusted PR it waits for the `Security Scan` check and mirrors its result
  (failure → the dependent CI jobs are skipped); trusted authors and non-PR
  events proceed immediately.

By trust tier (GitHub `author_association`):

- **Trusted** (`OWNER` / `MEMBER` / `COLLABORATOR`) and all non-PR events
  (push, schedule, dispatch): the gate passes through instantly, no scan.
- **Returning contributor** (`CONTRIBUTOR`): the gate runs the scan; a clean
  result lets CI proceed automatically, a finding blocks all CI.
- **First-time contributor**: GitHub's native *“require approval to run fork
  pull request workflows”* repo setting already holds every workflow until a
  maintainer clicks **Approve and run**; after approval the gate's scan still
  applies.

The scan inspects the PR diff for committed secrets, secret-exfiltration shapes
(a secret-named credential source plus a network sink in one file, an
`os.environ` dump, a decode-then-exec, or a reverse shell), changes to
privileged repo config (CI workflows, `.github/MAINTAINER`, `CODEOWNERS`,
`.github/scripts`), CI-workflow misuse (`pull_request_target` + PR-head
checkout, unpinned actions), and known code-execution / obfuscation patterns
(semgrep, local ruleset). It only *statically* analyses the diff and runs with
**no secrets** on fork PRs,
and the scanner itself always runs from `main`, so a PR cannot weaken its own
scan.

This is **not** a merge-required check: it gates CI, not the merge button
directly. When enforcing, merge stays blocked transitively (the skipped
pytest/e2e checks are required) and `Maintainer Approval` remains the ultimate
gate.

It is **blocking**: a finding fails the `Security Scan` check, the pollers mirror
that failure, and the dependent CI jobs are skipped. Detectors run fail-fast, so
a clean PR must pass every one.

### Maintainer override

A maintainer can waive the scan on a specific PR with the **`skip-security-scan`**
label (same convention as `skip-e2e-ui-test`). The waiver is only honored when it
is *maintainer-effective*: the label is present **and** the PR author is a
maintainer, or a maintainer's latest decisive review is `APPROVED`. The label
alone does nothing — applying labels needs triage access, and the extra
maintainer check is defence in depth — so a fork contributor cannot self-waive.
The label and review state are read from the API, and the decision runs from
`should-scan.sh` on `main`, so a PR cannot edit the waiver logic.

To use it: a maintainer reviews/approves the PR and applies `skip-security-scan`;
the `Security Scan` check re-runs and passes, then the blocked CI workflows are
re-run (or the contributor pushes) so their gate jobs see the now-green scan.
The waiver stays effective across pushes while the maintainer approval stands —
remove the label (or dismiss the approval) to re-enable scanning.
