# Handling CI & PR Reviews for External Contributors

## Goal
Let external (fork) contributors run meaningful CI while protecting secrets (LLM keys, the test-gateway token) and keeping `main` stable — without undue maintainer burden.

## Prerequisite: security-scan guard + contributor gating
Before any CI runs on a fork PR:
- **First-time contributors require maintainer approval** (GitHub's native "require approval for outside/first-time contributors" setting).
- **Security-scan guard.** The [Security Gate](https://github.com/omnigent-ai/omnigent/pull/269) (`.github/workflows/security-gate.yml`) — a reusable, **no-secrets** deterministic scan of the PR diff — runs as the first job of each CI workflow; the real jobs `needs: gate`, so a failing gate skips them and untrusted code is never checked out, built, or run on our runners. The scanner is always checked out from `main`, so a PR can't weaken its own gate. Defense-in-depth, not a guarantee.
- **Abuse handling.** Automate *detection/flagging* of repeat spam/attack PRs, but keep the *ban* a maintainer action (a denylist the workflow checks) to avoid false positives.

## Attack surface — applies to every option

Once any fork code runs on our runners, the core primitive it obtains is **arbitrary code execution on the runner** — secrets are only one of the things worth stealing once that's true. These vectors exist regardless of which option below we adopt; what each option changes is *when* fork code runs, *with what in scope*, and *what gates it*. The per-option Pros/Cons reference these two groups.

**(a) Vectors that depend on a secret being in scope:**
- **Secret exfiltration.** The job environment holds the LLM/test-gateway credentials; arbitrary code in the PR can read them and ship them out — print to build logs, POST to an attacker endpoint, DNS-encode, or stash them in an uploaded artifact. The scan can't catch every sink (DNS, artifact upload, a dependency's post-install hook, indirection across files).
- **Credential abuse on the spot.** Even without exfiltrating, the code can *use* the key during the run — burn LLM quota, drive cost, or hammer the gateway as a DoS/abuse vector.

**(b) Vectors that work *even with zero secrets in scope* — they need only code execution, so removing the keys does not remove them:**
- **Supply-chain / dependency execution.** A PR can bump a lockfile or add a dependency whose install/import hook runs attacker code at full privilege — a diff-text scan flags the manifest change at most, not the remote payload. This *is* the RCE primitive; everything else in this group builds on it.
- **Cache poisoning.** A fork run (keyed or not) can write to the Actions cache; a later *trusted* run on `main` restores that cache, letting attacker-controlled content execute in a privileged context — lateral movement past the fork sandbox.
- **Compute abuse / cryptomining.** The runner is free compute with outbound network; mining needs no secret, just CPU. Bounded on GitHub-hosted runners (time/concurrency limits, active mining detection) but still burns minutes and degrades queue time.
- **CI-system DoS.** Many pushes/PRs exhaust runner concurrency and starve the queue, denying CI to legitimate PRs — a pure availability attack that scales with PR churn.
- **Artifact poisoning → privileged consumer.** A no-secret fork job emits a build artifact that a later, more-privileged `workflow_run` / release workflow downloads and trusts → code runs in privileged context (the classic "pwn request via artifact" chain). Trigger chaining (fork `pull_request` run sets state a `pull_request_target`/`workflow_run` consumes) is the same shape.
- **`GITHUB_TOKEN` abuse.** Even the read-only fork token enables API scraping, recon, and rate-limit hammering; the read-write path (the `pull_request_target` mirror) is the dangerous one.

### Standing platform constraints (option-independent)
- What's exposed is a rate-limited, revocable test-gateway token, not raw LLM/prod keys — this bounds the blast radius of group (a), but does **nothing** for group (b).
- All CI runs on **GitHub-hosted `ubuntu-latest` runners — no self-hosted runners** (verified across `.github/workflows/`). This is the single biggest reason group (b) isn't catastrophic: there's no persistent runner state to backdoor and no private-network foothold to pivot from. Treat it as a **standing constraint** — a self-hosted runner reachable from fork CI would sharply escalate group (b).

### Baseline CI controls for group (b), and current status
Each secret-independent vector maps to a standard CI control. These controls apply no matter which option is chosen; statuses are from a `.github/workflows/` audit:

| Vector | CI control | Status in omnigent (audited) |
|---|---|---|
| Supply-chain / dependency execution | Don't run untrusted code in a privileged context (the gate); read-only token + no secrets on the auto-run tier; locked/hash-pinned deps; SHA-pin all actions; egress monitoring | **Partial** — actions are SHA-pinned; the [Security Gate](https://github.com/omnigent-ai/omnigent/pull/269) flags manifest changes; no runner egress monitoring yet |
| Cache poisoning | Keep fork-PR cache writes out of any key a trusted run restores | **Covered** — on fork `pull_request`, `e2e.yml`/`e2e-ui.yml` run only a `setup` job that computes an *empty* shard matrix (no cache-writing shard jobs run; forks run the real suite via the trusted `fork-e2e/**` mirror); `ci.yml`/`lint.yml` do let fork PRs write caches but rely on GitHub's native branch-scoped cache isolation (fork-PR caches aren't readable by trusted `main` runs) |
| Compute abuse / cryptomining | First-time-approval gate + `timeout-minutes` + concurrency caps; hard-bounded by GitHub-hosted-only | **Covered** — first-time gate (proposed), `timeout-minutes` on all 20 workflows, no self-hosted runners |
| CI-system DoS / queue starvation | `concurrency:` with `cancel-in-progress`; `timeout-minutes`; abuse denylist | **Strong** — `concurrency:` in 18/20 workflows; abuse denylist in this proposal |
| Artifact poisoning → privileged consumer | In `workflow_run` workflows treat downloaded artifacts as untrusted **data, never execute**; run only base-repo scripts; no fork-head checkout | **Verified safe** — all 3 `workflow_run`-triggered consumers comply: `code-coverage` reads only `total.txt`; `merge-ready` sparse-checks-out base-repo scripts (`persist-credentials: false`, fork JSON via env not interpolation); `maintainer-approval-rerun-run` only calls the re-run API |
| `GITHUB_TOKEN` abuse | Least-privilege top-level `permissions:`; avoid `pull_request_target` except privilege-separated | **Strong** — all 20 workflows declare `permissions:`; fork `pull_request` token is read-only; the writable-token `workflow_run` consumers run base code only; the one `pull_request_target` is the privilege-separated `fork-e2e-mirror` |

The one residual hardening item is **runner egress monitoring** (e.g. step-security/harden-runner) on whichever tier auto-runs fork code, to detect exfiltration and mining outbound that a static diff scan can't see.

---

After the security check, the three options differ in how they trade contributor experience against these vectors:

---

## Option 1 — Run everything (incl. LLM-key e2e) on every PR once the scan passes

**Pros**
- Simplest contributor experience: full signal automatically, no maintainer action to trigger e2e.
- Fastest feedback loop — no waiting on a human to post a command.

**Cons**
- **Maximally exposes both vector groups.** Option 1 auto-runs the *secret-bearing* tier on every push with only the static scan as the gate — so keys are in scope for arbitrary fork code (**group (a)** in full), and that code also runs unreviewed on every push (**group (b)**). It's the only option that leaves group (a) ungated.
- **The scan can't be the sole gate on *triggering key-bearing CI*.** The [Security Gate](https://github.com/omnigent-ai/omnigent/pull/269) is a deterministic, diff-text scan (secret detection, sensitive-path and workflow-misuse checks, a semgrep ruleset) — it doesn't execute code, and static pattern matching over added lines can be obfuscated past. Under Option 1 it is the *only* thing deciding whether arbitrary contributor code runs with keys in scope. (Distinct from merge protection — `Maintainer Approval` still gates merge regardless; the risk here is execution-with-secrets at CI-trigger time, before any merge.)
- Per the Security Gate's own trust tiers, a returning `CONTRIBUTOR` passes the scan and CI proceeds **automatically with no human review** — so for that tier the deterministic scan is the *only* thing between contributor code and the key-bearing jobs.
- **Uncapped runs on every push** multiply both groups (cost + abuse surface scale with PR churn).
- The standing constraints above blunt the impact (the revocable token caps group (a); no self-hosted runners caps group (b)) but don't make scan-only auto-gating sound — a diff-text scan still can't safely decide whether arbitrary code may execute with secrets at all.

---

## Option 2 — Auto-run non-key tests; maintainer reviews, then applies an `e2e-approved` label ✅ Recommended

**Pros**
- Industry-standard pattern (`ok-to-test`-style labels, environment protection rules).
- Secrets only reach fork code *after* a human has read the diff — maintainer review is the primary gate.
- Fast feedback on cheap tests; expensive/sensitive run is gated; `main` stays green.
- Already supported by our `fork-e2e-mirror.yml` (privilege-separated: the privileged workflow never runs fork code; fork code runs with secrets only on the trusted `push` to `fork-e2e/pr-N`).
- **A label is the cleaner trigger than a `/e2e` comment.** Applying a label is itself permission-gated — only users with triage/write access can add labels — so the maintainer action is authenticated by GitHub's permission model. A comment trigger (`issue_comment`) fires for *anyone*, so it would need an explicit author-allowlist check in the workflow (the pattern HF Transformers' `run-slow` uses); the label avoids that entirely. It also leaves a persistent, visible state on the PR (re-evaluated on each sync) rather than a one-shot comment event, matching the existing `security-scan-override` label mechanism.
- **Addresses both vector groups.** Group (a): secrets reach fork code only *after* a human has read the diff. Group (b): the auto-run tier is secret-free and — per the baseline controls above (no fork-artifact execution, branch-scoped caches, least-privilege tokens) — can't reach the privileged paths. The audit found no unguarded fork→trusted path; the one residual is runner egress monitoring.

**Cons**
- Adds a manual step — maintainer must apply the `e2e-approved` label; review latency can bottleneck merges.
- e2e issues surface later in the cycle (after initial review), not on first push.
- *Implementation note:* add `labeled` to `fork-e2e-mirror.yml`'s `pull_request_target` `types:` and have `should-mirror.sh` open when the `e2e-approved` label is present (alongside the existing maintainer/returning-contributor conditions). `pull_request_target` runs the gate from the trusted base ref and receives the secrets needed to mint the mirror App token, so the labeled fork PR's merge commit runs keyed e2e on the trusted `fork-e2e/pr-N` push — no `repository_dispatch` or comment-parser needed. Re-strip the label (or re-require it per push) if you want each new commit re-gated.

---

## Option 3 — Pre-merge non-e2e only; e2e runs post-merge on `main`, revert/auto-fix on break ↩️ Fallback

**Pros**
- Fastest path to merge — no pre-merge e2e wait or maintainer trigger.
- Keeps the PR loop light; e2e cost moves off per-PR.

**Cons**
- Trades away pre-merge confidence — `main` can break.
- Reverts create churn and a poor contributor experience.
- "Auto-file a fix" for an LLM e2e failure is optimistic — these failures are often flaky/semantic, not mechanically fixable.
- **Vector profile.** The pre-merge tier still auto-runs fork code, so group (b) exposure matches Option 2's cheap tier. Group (a) shifts *post-merge*: e2e runs on `main` with secrets after merge, so `Maintainer Approval` at merge is the only gate before keyed execution, and any malicious change then runs in the trusted `main` context rather than an isolated mirror.

---

## Recommendation
Adopt **Option 2**, built on the existing `fork-e2e-mirror.yml` privilege-separation, with the security scan as defense-in-depth and **maintainer review as the primary gate** before any key-bearing run — triggered by an `e2e-approved` label (permission-gated, no author-allowlist needed) rather than a `/e2e` comment. Fall back to Option 3 only if label-review latency becomes the real bottleneck.

---

## Appendix: How other popular LLM/AI projects handle this

Surveyed six widely-used OSS LLM/AI projects to validate the approach above. The findings strongly support **Option 2** — *no* surveyed project gives fork code automatic access to secrets, and they all gate the expensive/secret tier behind either a maintainer action or move it off the PR path entirely.

### Platform baseline (true for all)
1. **Fork `pull_request` runs get a read-only token and no secrets.** Same-repo branch PRs do get secrets (author already has write access). ([github.blog](https://github.blog/news-insights/product-news/github-actions-improvements-for-fork-and-pull-request-workflows/), [securitylab](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/))
2. **First-time / outside-contributor runs require manual maintainer approval** (a repo Actions setting; GitHub recommends the stricter "all outside collaborators" for public repos). ([docs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks))
3. **`pull_request_target` is the footgun** — it runs base-branch workflow code with secrets even for forks; the dangerous anti-pattern is combining it with an explicit checkout of untrusted PR head. ([wellarchitected](https://wellarchitected.github.com/library/application-security/recommendations/actions-security/))

### Comparison

| Project | Returning contrib auto-CI? | e2e/keys for contribs? | Merge process | Demonstration | What gates *running secret-bearing tests* on a fork PR (NOT the merge gate) |
|---|---|---|---|---|---|
| **vLLM** | Expensive: No | Gated by `ready` label | auto-merge + `ready` | [`docs/contributing/README.md`](https://github.com/vllm-project/vllm/blob/main/docs/contributing/README.md) (policy) + [`.buildkite/`](https://github.com/vllm-project/vllm/tree/main/.buildkite) (job defs only) | The full (secret/expensive) Buildkite pipeline runs only once a reviewer applies the `ready` label; fork PRs otherwise get `fastcheck` only. The label→full-build trigger lives in **Buildkite's pipeline settings, not in the repo** — `.buildkite/` only defines the jobs (`ci_config.yaml`, `test_areas/`) with no in-repo `ready` conditional. |
| **HF Transformers** | Expensive: No | Gated by maintainer allowlist | manual maintainer merge | [`self-comment-ci.yml`](https://github.com/huggingface/transformers/blob/main/.github/workflows/self-comment-ci.yml) | The `HF_TOKEN`/GPU job runs only `on: issue_comment` and its `if:` is an **AND**: issue open `&&` `github.actor` ∈ a hardcoded ~20-name maintainer allowlist `&&` body starts with `run-slow`. The real gate is the **actor allowlist** — `run-slow` is just the command keyword (anyone can type it; only an allowlisted maintainer's comment triggers the keyed job). No fork `pull_request` event can reach it. |
| **OpenClaw** | Cheap: Yes — [no first-time gate](#empirical-verification--observed-behavior-on-real-fork-prs-june-2026) | No on PRs (off-PR + command-gated) | maintainer review + squash | [`ci.yml`](https://github.com/openclaw/openclaw/blob/main/.github/workflows/ci.yml) + [`openclaw-live-and-e2e-checks-reusable.yml`](https://github.com/openclaw/openclaw/blob/main/.github/workflows/openclaw-live-and-e2e-checks-reusable.yml) + [`mantis-telegram-live.yml`](https://github.com/openclaw/openclaw/blob/main/.github/workflows/mantis-telegram-live.yml) | `ci.yml` runs on fork `pull_request` and references **zero secrets**. The live/e2e tier (`openclaw-live-and-e2e-checks-reusable.yml`, ~40 provider keys — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) declares only `workflow_call` + `workflow_dispatch` — **no `pull_request`/`pull_request_target`** — so a fork PR can't start it. All four of its callers (`openclaw-scheduled-live-checks` = cron + dispatch; `openclaw-release-checks`, `package-acceptance`, `plugin-prerelease` = dispatch-only) are likewise schedule/dispatch, and `workflow_dispatch` requires repo write access — so the keyed tier runs only on the nightly schedule or a maintainer's manual dispatch. Separately, `mantis-telegram-live.yml` exposes a `@openclaw-mantis` comment command gated by a `getCollaboratorPermissionLevel` check (admin/maintain/write) **plus** `environment: qa-live-shared` protection. |
| **LiteLLM** | Cheap: Yes — [no first-time gate](#empirical-verification--observed-behavior-on-real-fork-prs-june-2026) | No (mocked-only) | CLA + ≥1 test + green CI | [`.circleci/config.yml`](https://github.com/BerriAI/litellm/blob/main/.circleci/config.yml) | Every key-bearing CircleCI job carries `filters: branches: only: [main, /litellm_.*/]`; a fork PR's branch name can't match, so those jobs never run on fork PRs (CircleCI also doesn't run at all on forks without setup). |
| **LangChain** | Cheap: Yes — [no first-time gate](#empirical-verification--observed-behavior-on-real-fork-prs-june-2026) | No on PRs | CODEOWNERS + merge queue | [`integration_tests.yml`](https://github.com/langchain-ai/langchain/blob/master/.github/workflows/integration_tests.yml) | The keyed integration suite is `on: schedule` + `workflow_dispatch` only (**no `pull_request`**), so a fork PR can never trigger it. The job `if: github.repository_owner == 'langchain-ai' \|\| github.event_name != 'schedule'` additionally blocks forks from the *scheduled* run while allowing manual dispatch. |
| **Ollama** | Cheap: [first-timers gated, returning auto](#empirical-verification--observed-behavior-on-real-fork-prs-june-2026) | No | GitHub UI (undocumented) | [`test.yaml`](https://github.com/ollama/ollama/blob/main/.github/workflows/test.yaml) (PR CI) + [`release.yaml`](https://github.com/ollama/ollama/blob/main/.github/workflows/release.yaml) (release pipeline) | The PR CI that fork PRs actually run — `test.yaml` (`on: pull_request`) — references **zero secrets** (verified), so there's no keyed tier on the PR path to gate. The only secret-bearing workflows are the *release* pipeline (`release.yaml` on `push:` `v*` tags, `latest.yaml` on `release`), whose signing jobs are `environment: release`-scoped — unreachable from any PR. So Ollama has no secret-test-on-PR gate because it runs no secret tests on PRs at all. |
| **omnigent (us)** | **Yes (returning approved)** | **Yes, with keys** (via mirror) | `maintainer-approval` + `merge-ready` | `.github/workflows/fork-e2e-mirror.yml` | The keyed e2e tier runs only after `should-mirror.sh` opens (author is maintainer **OR** a maintainer applies the `e2e-approved` label **OR** `fork-e2e/pr-N` already exists, i.e. returning contributor) **and** the diff security scan passes; the mirror then pushes fork HEAD to the trusted `fork-e2e/pr-N` branch where `e2e.yml` runs with keys (forks get an empty matrix on the `pull_request` path). |

*(PyTorch and llama.cpp dropped from the survey at maintainer request; LlamaIndex omitted — no verified-quality public evidence surfaced.)*

### The four gating techniques observed
1. **No `pull_request` trigger on the secret tier** — fires only on tags/schedule/dispatch (LangChain, OpenClaw's scheduled live checks, Ollama's tag-only release).
2. **Branch-name filter** fork branches can't satisfy (LiteLLM).
3. **Event + identity `if:` guard** — a maintainer command on `issue_comment` whose author is checked against an allowlist (Transformers' `run-slow`) or a live `getCollaboratorPermissionLevel` call (OpenClaw's `@openclaw-mantis`). The author check is mandatory precisely *because* anyone can comment.
4. **Environment protection** — secret-bearing jobs sit behind a GitHub Environment (`environment: release` in Ollama, `environment: qa-live-shared` in OpenClaw), which can require reviewers/branch rules before the secrets are exposed.

### Implications for this proposal
- **Industry consensus validates Option 2.** The dominant pattern is exactly what Option 2 proposes — fast/mocked checks auto-run on forks; the secret/expensive tier is gated behind a *maintainer action that runs in a trusted context*. Our `e2e-approved` label maps directly onto vLLM's `ready` label; it's the label-based variant of the command-based gates (Transformers' `run-slow`, OpenClaw's `@openclaw-mantis`). The advantage over those commands: a label is permission-gated by GitHub's collaborator model, so we avoid the explicit author/permission check those projects must run on every comment (HF's hardcoded allowlist, OpenClaw's `getCollaboratorPermissionLevel`).
- **No peer extends secret-tier trust based on past approval.** Every surveyed project re-gates the expensive tier **per PR regardless of tenure**, or never runs it on PRs. Our `fork-e2e-mirror` returning-contributor shortcut (`fork-e2e/pr-N` exists → auto-mirror without fresh approval) is an **outlier** — it grants the keyed e2e tier to previously-approved forks without a fresh human gate. This is the Option 1 risk surface re-introduced for returning contributors and should be a conscious decision: either re-gate it per PR to match the norm, or document it as an accepted risk justified by the rate-limited, revocable test-gateway token.
- **Our privilege-separation is more advanced than most.** Where peers *withhold* keyed tests from fork code, our `pull_request_target` → trusted-mirror → `push` relay lets the keyed suite actually run on contributor code safely. That capability is what makes Option 2 low-friction for us — but it only stays safe if the trigger gate (maintainer review) is preserved.

### Empirical verification — observed behavior on real fork PRs (June 2026)

The repo Actions approval setting is private, but the *behavior* leaves an observable trace: a fork PR awaiting "Approve and run" shows its workflow runs in GitHub's **`action_required`** state. Correlating that status with the author's tenure (first-time vs returning contributor) on real, current PRs lets us read each project's effective policy directly rather than inferring it from config alone.

| Project | First-time contributor fork PR | Returning contributor fork PR | Verdict |
|---|---|---|---|
| **Ollama** | [#16744](https://github.com/ollama/ollama/pull/16744) `river-martin` (none): `test` **`action_required`** | [#16711](https://github.com/ollama/ollama/pull/16711) `rick-github`, [#16651](https://github.com/ollama/ollama/pull/16651) `gabe-l-hart`: `test` **= success** | **Approval required for first-timers only** |
| **OpenClaw** | [#93564](https://github.com/openclaw/openclaw/pull/93564) `Antisubmissivist` (NONE): full `ci.yml` suite **auto-ran** (30 checks completed, no `action_required`); also [#93558](https://github.com/openclaw/openclaw/pull/93558), [#93545](https://github.com/openclaw/openclaw/pull/93545) | [#93576](https://github.com/openclaw/openclaw/pull/93576) `LiuwqGit` (CONTRIBUTOR) & [#93569](https://github.com/openclaw/openclaw/pull/93569) `openperf` (MEMBER): **identical** — `ci.yml` auto-ran, same checks | **No first-time gate; CI is the same for everyone** — `ci.yml` is secret-free, and the keyed live tier is off-PR (schedule) + maintainer-command-gated, so tenure changes nothing |
| **HF Transformers** | [#46685](https://github.com/huggingface/transformers/pull/46685) `puwaer` (`NONE`): main `PR CI` **ran automatically** (27 checks completed); `Build PR Documentation` + `Self-hosted runner (benchmark)` **`action_required`** | [#46686](https://github.com/huggingface/transformers/pull/46686) `kaixuanliu` (`CONTRIBUTOR`): `PR CI` ran (30 completed) | **No first-time gate on main CI**; on the first-timer PR the doc-build + benchmark jobs are `action_required` because they're environment-gated (tenure-independent by mechanism, not a first-time gate) |
| **LiteLLM** | [#30509](https://github.com/BerriAI/litellm/pull/30509) `TokenMixAi` (0 merged): GH Actions **completed, no gate** | [#30479](https://github.com/BerriAI/litellm/pull/30479) `lucassz` (CONTRIBUTOR): auto-ran | **No GH-Actions gate**; CircleCI simply doesn't run on forks (0 circleci contexts vs 48 on internal PRs) |
| **LangChain** | [#38150](https://github.com/langchain-ai/langchain/pull/38150) `vsingh45` (0 merged): `check_diffs` CI **auto-ran** | [#38145](https://github.com/langchain-ai/langchain/pull/38145) `isatyamks` (returning): auto-ran | **No first-time gate**; live-key tests are off-PR anyway |
| **vLLM** | [#45764](https://github.com/vllm-project/vllm/pull/45764) `baolongsun` (0 merged): `pre-commit` **ran** | [#45782](https://github.com/vllm-project/vllm/pull/45782) (no `ready` label): only `pre-commit`, **no full CI** | **No GH-Actions gate**; real CI on Buildkite, gated by `ready` label regardless of tenure |

**Two camps — and the dividing line is *what runs automatically*, not tenure:**

1. **Lean on GitHub's native first-time-approval gate** (Ollama). Its auto-run tier includes builds that could touch infra, so it keeps the native gate **on** — first-timers blocked until "Approve and run," returning contributors flow automatically. The classic default.
2. **Don't gate first-timers at all** (Transformers, LiteLLM, LangChain, vLLM, OpenClaw). A confirmed first-timer's main CI ran with **zero approval** — and, where checked (OpenClaw), returning contributors get the *identical* run, confirming tenure is not the lever. They can afford this because the auto-run tier is provably secret-free (mocked/unit/lint/compile) and the dangerous tier is isolated by a *different* mechanism — environment protection (Transformers doc-build/benchmark; OpenClaw's `qa-live-shared`), a separate CI system that doesn't run on forks (LiteLLM CircleCI), off-PR triggers (LangChain; OpenClaw's scheduled live checks), label-gating (vLLM Buildkite `ready`), or a permission-checked command (OpenClaw's `@openclaw-mantis`).

**Why this sharpens the proposal:** the projects that let *anyone* — even first-timers — auto-run CI all share one property: **their auto-run tier cannot reach a secret**, so approval gating is unnecessary there. The gate (native first-time approval, or our `e2e-approved` label + mirror) exists *only* to guard the secret-bearing tier. This confirms Option 2's structure empirically and reinforces the outlier finding above: **no surveyed project auto-runs the secret tier for returning contributors** — even the "ungated" camp only ungates because that tier has no secrets. Our `fork-e2e-mirror` returning-contributor shortcut remains the lone exception, precisely because it auto-runs the *keyed* tier.
