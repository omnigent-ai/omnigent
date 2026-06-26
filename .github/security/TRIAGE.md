# Security alert triage

How Dependabot and CodeQL (code-scanning) alerts are managed for this repo.

## Pipeline

| Layer | Mechanism | What it does |
|---|---|---|
| Detection — deps | Dependabot alerts (on) | Flags vulnerable dependencies. |
| Detection — code | CodeQL default setup (on) | Flags code-level findings. |
| Detection — secrets | Secret scanning + push protection (on) | Blocks committed secrets. |
| Detection — diff | `security-scan.yml` | Per-PR static scan (secrets/exfil/sensitive-path/workflow-misuse/semgrep/OSV). |
| **Fixing — deps** | **Dependabot security updates** + `dependabot.yml` | Auto-opens grouped fix PRs for vulnerable deps. |
| **Triage** | **`security-triage.yml`** (this) | Daily AI triage: dismiss high-confidence false positives, escalate serious findings privately. |

Dependency *fixing* is Dependabot's job; this workflow does not edit code. Code
findings are never auto-fixed — only triaged.

## How the triage cron decides

The cron (`.github/workflows/security-triage.yml`) follows the same
injection-resistant model as `issue-triage.yml`: trusted steps fetch alerts and
apply mutations; the LLM (`.github/triage/security/`) runs with **no tools, no
shell, no token** and only emits validated JSON.

Per alert the model returns one of:

- **false_positive** — pattern not exploitable here (must name why).
- **wont_fix** — real but negligible (test-only fixture / dev-only tooling).
- **serious** — real and exploitable in production / on untrusted input.
- **monitor** — uncertain; left for a human.

Mutations are tightly gated:

- **Auto-dismiss** happens only at **confidence ≥ 0.9**, and is allow-listed
  on each side:
  - **CodeQL** — only for an allow-listed set of rule ids (see
    `AUTO_DISMISS_RULES` in the workflow). `py/path-injection` and
    `actions/untrusted-checkout` are **not** auto-dismissable.
  - **Dependabot** — only **low/medium** severity advisories. A **high or
    critical** dependency advisory is never auto-dismissed on the model's word
    alone; it always waits for a human.
- **serious** findings are collected into a **private** GitHub Security
  Advisory draft. They are never posted to public issues.
- **Mutations are OFF by default.** APPLY mode requires either the repo
  variable `SECURITY_TRIAGE_APPLY == 'true'` (enables scheduled enforcement) or
  a manual dispatch with `dry_run` unchecked. Merging the workflow alone never
  triggers a live run — review a few dry-run summaries first.

## Tokens

- CodeQL dismissals use the job `GITHUB_TOKEN` (`security-events: write`).
- Dependabot dismissals and advisory creation need a repo/org secret
  **`SECURITY_TRIAGE_TOKEN`** (fine-grained PAT with *Dependabot alerts:
  write* + *Security advisories: write*) — `GITHUB_TOKEN` cannot do either.
  Without it the cron still classifies and reports; it just can't mutate
  Dependabot alerts or open advisories.

## Verified false positives (current backlog)

These were checked by reading the code during the initial audit and are safe to
dismiss as false positives:

- `py/clear-text-logging-sensitive-data` @ `omnigent/inner/claude_sdk_executor.py`
  — the `logger.info` logs `model / gateway / base_url / tool-count`, no secret.
- `py/weak-sensitive-data-hashing` @ `omnigent/model_catalog.py:225` — SHA256 is
  used to build a non-secret 16-char **cache fingerprint**, not to store a
  password. The secret is deliberately never persisted.

Accepted-risk (review, then dismiss with justification — not silently):

- `actions/untrusted-checkout` (critical) @ `oss-regen-on-comment.yml` — the
  `issue_comment` workflow checks out PR head, but with `persist-credentials:
  false`, no token on disk during `uv lock`, an App token minted only after the
  lock and used only at the push step, behind an `authorize` gate. Untrusted
  code runs without secrets in scope.

Needs per-case review (do **not** bulk-dismiss): the 52 `py/path-injection`
findings in `spec/parser.py`, `tools/builtins/upload_file.py`, `spec/tar_utils.py`,
etc. — most are trusted-input, but the extraction paths deserve a look.

Serious (fix, don't dismiss): `starlette` and `cryptography` advisories (server
runtime); the `undici` cluster in `ap-web`.
