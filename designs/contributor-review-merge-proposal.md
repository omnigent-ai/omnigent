# Review & Merge Process for External Contributors

## Goal
Define how contributor (fork) PRs get **reviewed, approved, and merged** — keeping a maintainer in the loop on every merge while shrinking the per-PR review burden through automation. This is the *human review + merge gate* counterpart to the CI/secrets proposal ([`ci-external-contributors-proposal.md`](./ci-external-contributors-proposal.md)); that doc covers *what runs on a fork PR*, this one covers *who approves it and how it merges*.

## TL;DR
- **Every contributor PR keeps a maintainer approval before merge** — independent of PR size. Merge safety doesn't scale down with diff size, and the surveyed OSS projects don't relax it for small PRs either.
- **Automation does the heavy lifting *before* the human looks:** security-scan gate, an auto-triggered AI review (Copilot / Polly / Debby), standard CI, and a test-coverage check promoted to required (it's report-only today). By the time a maintainer opens the PR, the mechanical questions are already answered.
- **The maintainer's residual job is narrow and judgement-only:** (1) is this safe (catch malicious code the scan missed)? (2) do we actually want this change? Nothing else should require human attention.
- **Reviewer assignment is explicit** — round-robin by default, CODEOWNERS-routed where domain knowledge matters — so PRs don't stall unowned.
- **A separate abuse track keeps spam/malicious PRs out of review** — auto-flag, reversible auto-close, and a maintainer-confirmed denylist to skip CI from repeat offenders. Detection is automated; the ban stays a maintainer action.
- **Proven contributors can be promoted to collaborators** — a GitHub write-access grant (no new code) that skips the security scan and the fork path. It's a *separate* grant from `.github/MAINTAINER`: collaborators still can't approve merges until added there too.

---

## Current state (audited)
What already exists in `.github/workflows/`:
- **`maintainer-approval.yml`** — the `Maintainer Approval` status is a required check that stays red until a maintainer approves. Runs on `pull_request_target` from `main`, reads `.github/MAINTAINER` at main's tip (a PR can't self-grant), checks out no PR code. This is the merge gate we're building the human process around.
- **`merge-ready.yml`** — posts the single required `Merge Ready` status backing branch protection; supports `/merge` (write-access commenter) and the `automerge` label, and re-evaluates on CI completion. (There is no CI bypass label: land despite red required checks by quarantining the flaky test or via a repo admin's native bypass.)
- **`security-gate.yml` / `security-scan.yml`** — the no-secrets deterministic diff scan that gates CI (see the CI proposal).
- **`code-coverage.yml` / `ui-code-coverage.yml`** — currently **report-only**: posts a `Coverage` status whose % rides in the description, never required, can't block merge.
- **`pr-size.yml`** — applies `size/{XS..XL}` labels for reviewer triage only (informational; *not* a gate — see below).

**Gaps this proposal closes:** no explicit reviewer-assignment policy; no auto-triggered AI review; coverage is not enforced.

---

## Principle: merge safety is size-independent
Keep a maintainer approval on **every** contributor PR before merge, regardless of `size/*` label.

- A one-line diff can introduce a backdoor, a malicious dependency bump, or a subtle logic flaw as easily as a large one — often more easily, because small PRs invite shallow review. Risk correlates with *what* changes (sensitive paths, deps, auth, CI config), not line count.
- The `size/*` labels stay **informational only** — they help a maintainer budget review time, never auto-approve or bypass the gate.
- **OSS precedent agrees.** No surveyed project ([CI proposal appendix](./ci-external-contributors-proposal.md#appendix-how-other-popular-llmai-projects-handle-this)) waives maintainer approval for small contributor PRs — vLLM (`ready` + reviewer), HF Transformers (manual maintainer merge), OpenClaw (maintainer review + squash), LangChain (CODEOWNERS + merge queue), LiteLLM (CLA + green CI + reviewer). Size-based fast-pathing of *merge* is not an industry pattern; where size is used, it's for routing/labeling, not for skipping human approval. *(If we ever want a fast lane, scope it to docs/comment-only diffs verified by path, never by line count — but default is no exception.)*

---

## Reviewer assignment
A PR with no clear owner stalls. Two complementary routing modes:

1. **Knowledge-based (default where ownership is clear).** Adopt a `CODEOWNERS` file mapping subsystems → maintainers; GitHub auto-requests the right reviewer. This keeps domain-heavy changes (harnesses, auth, CI) with people who know them. *(None exists today — adding one is a prerequisite.)*
2. **Round-robin (fallback / load-balancing).** For PRs not covered by CODEOWNERS, or to spread load, auto-assign from the maintainer pool on a rotating basis (GitHub team round-robin assignment, or a small `pull_request_target` action reading `.github/MAINTAINER`).

Recommended: **CODEOWNERS first, round-robin for the remainder.** Add a stale-PR nudge (label + ping) so an unanswered contributor PR resurfaces rather than rotting.

---

## Make the human review cheap: automation before eyes
The goal is that when a maintainer opens a contributor PR, every *mechanical* question is already answered, leaving only judgement. Four pre-human gates:

| Check | Mechanism | Status today | Action |
|---|---|---|---|
| **Security scan** | `security-gate.yml` deterministic diff scan (secrets, sensitive paths, workflow misuse, semgrep) | Exists | Keep; defense-in-depth, not a guarantee — the maintainer is the backstop for what it misses |
| **AI review** | Auto-trigger Copilot review (or Polly / Debby) on PR open | Not wired up | Add — auto-request on `opened`/`synchronize` so a first-pass line review is waiting before the maintainer looks |
| **Test coverage** | `code-coverage.yml` | Report-only | **Promote to a required check** with a threshold (e.g. no net coverage regression on changed lines), so untested contributions are bounced automatically |
| **Standard CI** | `ci.yml` / lint / e2e (per CI proposal) | Exists | Keep as required |

With those green, the maintainer's review collapses to two judgement calls only:

1. **Is it safe?** — a sanity pass for malicious or risky code the static scan can't catch (obfuscated exfiltration, sneaky dependency/postinstall hooks, sensitive-path changes). The AI review surfaces candidates; the human decides.
2. **Do we want it?** — does this change fit the project's direction, design, and maintenance appetite? Purely a product/maintainership call no automation can make.

Everything else — style, obvious bugs, missing tests, secret leaks — should be caught and reported by automation *before* this point.

### Notes / open questions on automation
- **AI review tool choice** (Copilot vs Polly vs Debby) — pick one to start; they're not mutually exclusive but stacking adds noise. Recommend trialing one on a sample of recent contributor PRs first.
- **Coverage threshold tuning** — start lenient (no regression on *changed* lines) to avoid false-bouncing legitimate refactors; tighten later. Coverage on fork PRs already flows through the artifact path (`code-coverage.yml` consumes data, never runs PR code), so promotion to required is mechanically safe.
- **AI review must not gate merge** — it's advisory input to the human, never a required status (it's non-deterministic and prompt-injectable from PR content).

---

## Promoting trusted contributors to collaborators
The flip side of the abuse track: a contributor who repeatedly ships high-quality, well-tested changes shouldn't stay on the fork path forever. Offer a **contributor → collaborator ladder** so proven contributors gain write access and stop hitting the fork-PR friction.

**The mechanism already exists in the workflows — promotion is a GitHub-role action, not new code.** There are two distinct trust tiers today, and they grant different things:

| Tier | How it's recorded | What it bypasses |
|---|---|---|
| **Collaborator** (write access) | GitHub `author_association` becomes `COLLABORATOR`/`MEMBER`/`OWNER` automatically | The **security scan** (`should-scan.sh:121-123` skips these associations) and the **fork path** (their PRs are same-repo, so no `fork-e2e` mirror, secrets/full CI directly) |
| **Maintainer** (higher tier) | Listed in `.github/MAINTAINER` (checked in, 21 today) | Can **approve PRs for merge**, apply `e2e-approved`, and waive the scan via `skip-security-scan` |

- **What promotion to collaborator actually changes** (verified, no change needed): the moment a contributor is added as a collaborator, their `author_association` flips to `COLLABORATOR`, so `should-scan.sh` classifies them as a *trusted author* and **skips the security scan**. Their branches live in the repo (same-repo PRs), so the fork-specific gating — first-time-contributor approval and the keyed-e2e mirror — no longer applies; they get secrets/full CI directly. They move *into* the reviewer-routing pool (CODEOWNERS / round-robin) rather than being gated by it.
- **What it does NOT bypass.** The **merge gate is keyed on `.github/MAINTAINER`, not on `author_association`** — so a collaborator is *not* a maintainer. `Maintainer Approval` and the required checks (CI, coverage) still apply to their PRs, and a collaborator **cannot approve anyone's PR for merge** (including their own) until they're also added to `.github/MAINTAINER`. Write access removes the *fork + scan* friction; it does not remove the *human merge approval* gate. (Whether collaborators can self-merge after a maintainer approval is a branch-protection setting — recommend requiring a non-author maintainer approver.)
- **Two separate grants, deliberately.** Adding someone as a GitHub collaborator (skips scan, off the fork path) and adding them to `.github/MAINTAINER` (can approve merges) are independent decisions. A contributor can be promoted to collaborator for CI convenience *without* gaining merge-approval power — that's the recommended first rung.
- **Promotion criteria (suggested, tune to taste).** A sustained track record — several merged non-trivial PRs over time, consistently green CI and tests, constructive review interactions, and a maintainer sponsor. Keep it a deliberate, documented decision, not an automatic merge-count threshold — write access is a trust grant, not a reward counter.
- **OSS precedent.** This is the standard committer ladder (contributor → collaborator → maintainer) used across major OSS projects. It's distinct from the [CI-proposal outlier finding](./ci-external-contributors-proposal.md#implications-for-this-proposal) — that warned against auto-extending the *secret-bearing CI tier* to a *fork* based on past approval; this is an explicit, human-decided *role grant* that moves the person off the fork path entirely, which is exactly the sanctioned mechanism. Note the security scan already encodes this distinction on purpose: returning `CONTRIBUTOR`s are still scanned ("a merged PR in the past does not vouch for the contents of this one"), and only the explicit `COLLABORATOR` role flips that off.
- **Reversible.** Both grants can be revoked (remove collaborator access; drop from `.github/MAINTAINER`) if trust lapses; pair promotion with periodic review of both lists.

## Abuse handling (separate track)
Keep abuse handling *out* of the normal review pipeline so spam/malicious PRs don't consume reviewer attention. Mirrors the CI proposal's stance: **automate detection/flagging, keep the ban a maintainer action.**

- **Auto-flag** likely-abuse PRs: security-gate hard failures, known spam patterns, throwaway accounts opening many low-effort PRs, PRs touching only sensitive paths with no rationale. Surface via label (e.g. `needs-triage`/`likely-spam`).
- **Auto-close** only on unambiguous signals (e.g. security-gate hard-fail with a templated explanation + link to contributing guide), reversible by a maintainer.
- **Maintainer-confirmed denylist** — a checked-in list the workflows consult to skip CI / auto-close from banned authors. The *ban* is a deliberate human action (avoids false-positive bans of legitimate contributors); the *enforcement* is automatic.
- **Why a maintainer in the loop on the ban:** an over-eager auto-ban that hits a real contributor is worse than the spam it prevents.

---

## Recommendation
1. **Keep maintainer approval required on every contributor PR before merge** — no size-based exemption (`maintainer-approval.yml` already enforces this; document the size-independence explicitly).
2. **Add reviewer routing** — CODEOWNERS for owned subsystems + round-robin fallback + stale-PR nudges.
3. **Front-load automation** — auto-trigger an AI review on open, and **promote coverage to a required check** with a changed-lines threshold, so the human review reduces to "is it safe?" and "do we want it?".
4. **Offer a contributor → collaborator ladder** — promote proven, high-trust contributors to write access (deliberate, documented, reversible) so they leave the fork path; this removes fork friction but keeps branch protection and the approval gate.
5. **Stand up the abuse track** — auto-flag + reversible auto-close + maintainer-confirmed denylist, kept separate from normal review.

This preserves the merge-time safety guarantee the CI proposal depends on (maintainer review is the primary gate before any keyed run) while making each contributor PR cheap for maintainers to process.
