# Handling CI & PR Reviews for External Contributors

## Goal
Let external (fork) contributors run meaningful CI while protecting secrets (LLM keys, the test-gateway token) and keeping `main` stable — without undue maintainer burden.

## Prerequisite: security-scan guard + contributor gating
Before any CI runs on a fork PR:
- **First-time contributors require maintainer approval** (GitHub's native "require approval for outside/first-time contributors" setting).
- **Security-scan guard.** The [Security Gate](https://github.com/omnigent-ai/omnigent/pull/269) (`.github/workflows/security-gate.yml`) — a reusable, **no-secrets** deterministic scan of the PR diff — runs as the first job of each CI workflow; the real jobs `needs: gate`, so a failing gate skips them and untrusted code is never checked out, built, or run on our runners. The scanner is always checked out from `main`, so a PR can't weaken its own gate. Defense-in-depth, not a guarantee.
- **Abuse handling.** Automate *detection/flagging* of repeat spam/attack PRs, but keep the *ban* a maintainer action (a denylist the workflow checks) to avoid false positives.

After the security check, three options:

---

## Option 1 — Run everything (incl. LLM-key e2e) on every PR once the scan passes

**Pros**
- Simplest contributor experience: full signal automatically, no maintainer action to trigger e2e.
- Fastest feedback loop — no waiting on a human to post a command.

**Cons**
- The scan can't be the sole gate on *triggering key-bearing CI*. The [Security Gate](https://github.com/omnigent-ai/omnigent/pull/269) is a deterministic, diff-text scan (secret detection, sensitive-path and workflow-misuse checks, a semgrep ruleset) — it doesn't execute code, and static pattern matching over added lines can be obfuscated past. Under Option 1 it is the *only* thing deciding whether arbitrary contributor code runs with keys in scope, so it can't be trusted to make that safe on its own. (This is distinct from merge protection — `Maintainer Approval` still gates merge regardless; the risk here is execution-with-secrets at CI-trigger time, before any merge.)
- Per the Security Gate's own trust tiers, a returning `CONTRIBUTOR` passes the scan and CI proceeds **automatically with no human review** — so for that tier the deterministic scan is the *only* thing standing between contributor code and the key-bearing jobs.

  **Attack vectors once a contributor's code runs with secrets in scope:**
  - **Secret exfiltration.** The job environment holds the LLM/test-gateway credentials; arbitrary code in the PR can read them and ship them out — print to build logs, POST to an attacker endpoint, DNS-encode, or stash them in an uploaded artifact. The scan can't catch every sink (DNS, artifact upload, a dependency's post-install hook, indirection across files).
  - **Credential abuse on the spot.** Even without exfiltrating, the code can *use* the key during the run — burn LLM quota, drive cost, or hammer the gateway as a DoS/abuse vector.
  - **Supply-chain / dependency execution.** A PR can bump a lockfile or add a dependency whose install/import hook runs attacker code at full privilege — a diff-text scan flags the manifest change at most, not the remote payload.
  - **Cache poisoning.** A key-bearing fork run can write to the Actions cache; a later *trusted* run on `main` restores that cache, letting attacker-controlled content execute in a privileged context — lateral movement past the fork sandbox.
  - **Token / CI abuse.** The job's `GITHUB_TOKEN` and runner can be turned to cryptomining, hitting internal endpoints, or probing other reachable services — all on every push, with no human in the loop.
- Uncapped key-bearing runs on **every push** multiply all of the above (cost + abuse surface scale with PR churn).
- *Mitigation:* what's exposed is a rate-limited, revocable test-gateway token, not raw LLM/prod keys — this bounds the blast radius of exfiltration/abuse, but does not address cache poisoning, supply-chain execution, or CI abuse, and doesn't make scan-only auto-gating sound.

---

## Option 2 — Auto-run non-key tests; maintainer reviews, then posts `/e2e` ✅ Recommended

**Pros**
- Industry-standard pattern (`/ok-to-test`, labeled triggers, environment protection rules).
- Secrets only reach fork code *after* a human has read the diff — maintainer review is the primary gate.
- Fast feedback on cheap tests; expensive/sensitive run is gated; `main` stays green.
- Already supported by our `fork-e2e-mirror.yml` (privilege-separated: the privileged workflow never runs fork code; fork code runs with secrets only on the trusted `push` to `fork-e2e/pr-N`).

**Cons**
- Adds a manual step — maintainer must post `/e2e`; review latency can bottleneck merges.
- e2e issues surface later in the cycle (after initial review), not on first push.
- *Implementation note:* the `/e2e` run must execute the PR's merge commit in a secret-bearing context — an environment with a required reviewer, or a maintainer-triggered `repository_dispatch`/mirror.

---

## Option 3 — Pre-merge non-e2e only; e2e runs post-merge on `main`, revert/auto-fix on break ↩️ Fallback

**Pros**
- Fastest path to merge — no pre-merge e2e wait or maintainer trigger.
- Keeps the PR loop light; e2e cost moves off per-PR.

**Cons**
- Trades away pre-merge confidence — `main` can break.
- Reverts create churn and a poor contributor experience.
- "Auto-file a fix" for an LLM e2e failure is optimistic — these failures are often flaky/semantic, not mechanically fixable.

---

## Recommendation
Adopt **Option 2**, built on the existing `fork-e2e-mirror.yml` privilege-separation, with the security scan as defense-in-depth and **maintainer review as the primary gate** before any key-bearing run. Fall back to Option 3 only if `/e2e` review latency becomes the real bottleneck.
