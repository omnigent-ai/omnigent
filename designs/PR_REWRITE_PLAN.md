# PR rewrite plan — slimmer, restructured routing PR

> **How to reference this document.** Every block carries an ID: section number + letter (`2c` = third block of §2). Speak the ID and it names the block.

**0a** Goal: replace PR #3506 with a rewritten branch that ships the same three CUJs (Smart Routing as a model choice on Claude Code and Codex; the Smart Routing harness; routed native subagent spawns) plus the CLI entry points, at a fraction of the current size, in a commit series a reviewer can actually read. This document is the plan Bryan critiques before the Opus fleet executes it.

**0b** Hard constraints. The rewrite starts from the current verified tree, not from scratch — every behavior on the branch is evidence-verified (registry: 15/15 matrix, live checks) and re-implementation would forfeit that. Cuts remove *scope*, not correctness. The final branch rebases onto current main (we are 201 commits behind again) and re-runs the §11 matrix before it replaces #3506.

## 1. What the PR is today

**1a** 28,991 insertions / 148 files against origin/main. Composition: python tests 9,591; web source 5,104; web tests 2,625; docs 2,593; server 2,461; runner+inner 3,054; adapters 1,394; cli+other 1,873; telemetry 296. Production source is ~9,100 lines; tests are 12,216 (42%); docs 2,593 (9%).

**1b** Largest single files: `runner/subagent_routing.py` +1,248; `server/smart_routing.py` +1,120; `NewChatDialog.test.tsx` +956; `orchestration.py` +886; `hook_scripts/subagent_router.py` +675; `cli.py` +505; `NewChatDialog.tsx` +488; `codex_executor.py` +484; `smart_routing_cli.py` +394 (the CLI workstream).

## 2. Keep-core: the minimum each CUJ needs

**2a** CUJ A (model choice on claude/codex): `smart_routing.py` (client, seam, arm menus, substitution), the orchestration turn gates, decision persistence as conversation items, the claude apply layer (vocabulary, alias pins, /model inject), the codex apply layer (settings push, config mirror, forwarder precedence), the chip rendering rules, the configure-dialog model option, gateway-backed gating (Bryan's explicit rule).

**2b** CUJ B (Smart Routing harness): `_resolve_native_smart_routing`, pre-session catalogs, `smart_routing_message`, harness row + persistence, cross-family subagents allowed only here.

**2c** CUJ C (routed subagent spawns): hook scripts + loopback relay + server policy (`resolve_subagent_route`), family constraints, per-session override with the Inherit row, codex hooks.json generation + trust handshake + `python -I`.

**2d** CLI (the new workstream, must survive the rewrite): `smart_routing_cli.py`, the `--smart-routing`/`-p` flags, the glm gateway-route fix (`907f8886`), and whatever tier-2/3 commits merge from the CLI worktree before we start.

## 3. Cut list — each with size, what is lost, and my recommendation

**3a** **Docs → separate PR (−2,593).** The four design docs move to a docs-only PR stacked on the code PR. Zero functionality lost. Recommendation: cut from the code PR. (We already did this dance once; this time it is a second PR, not untracked files.)

**3b** **The codex enforcement stack → defer to a follow-up PR (−~1,200 src + ~1,500 tests).** The canary, the enforcement watcher, the spawn audit + reconciliation, the warning banner (web + server halves), `session_warnings`, and the R8 machinery. This is observability for an *advisory* gate; it produced this branch's only false positive and its only open product decision (the missing-rewrite blind spot). Routing works without it — what is lost is *detection* of codex silently not running hooks. Recommendation: cut to a follow-up PR titled "enforcement observability"; keep only the hook-generation + trust handshake (CUJ C needs those). This is the single biggest source-side cut available.

**3c** **Cross-harness soft redirect → cut (−~150).** The deny-plus-"use sys_session_send" message. It is a product experiment; the A-sub verification showed models decline to follow it. Lost: any cross-family *delivery* under auto (the cross-family *decision* still records). Recommendation: cut; auto sessions constrain spawns to the resolved harness's family for v1.

**3d** **Fork-spawn exemption → cut (−~80).** Test-pinned only, never verified live, no user-visible surface. Recommendation: cut; forks inherit the session model implicitly.

**3e** **Telemetry events → keep (±296).** Small, review-hardened (family/tier labels only), and the managed swap references them. Recommendation: keep; not worth the churn to cut.

**3f** **Gateway-inference gating → keep in-PR but as its own commit (~900 src+tests).** Bryan's explicit product rule; also the PR's only migration. It is cleanly separable if the critique wants it as a stacked PR instead.

**3g** **Test rewrite, not test transplant (−~4,000–5,000 of 12,216).** The current tests accreted per-fix across three review waves: they pin intermediate states, duplicate coverage across consolidated files, and carry fixture scaffolding for deleted machinery. The fleet writes a fresh test suite against the *final* behavior per area (the coverage-gated method from `f8328623` worked; this time start from the behavior list, not the old files). Target: py tests ≤5,500, web tests ≤1,500. Lost: nothing, if coverage-gated against the keep-core behavior inventory (the registry §2 is that inventory).

**3h** **Web slimming (−~1,000 of 5,104).** With 3b the banner, its availability plumbing, and their tests go; the dead-code deletions already happened in review. `NewChatDialog.tsx`'s remaining +488 is mostly the harness row + gating + persistence — keep. Recommendation: no further web cuts beyond what 3b implies.

**3i** **Things explicitly NOT cut** (each was questioned once): honest `applied=false` records and the raw/applied chip (caught two real bugs); the `MODEL_LISTS` fork (substitution needs a catalog-less cost ordering — flagged for the reviewer in the commit message); the layered redirect (ours encodes observed 400s the catalog cannot); session-start cadence machinery (it IS the simple path now).

**3j** Net size estimate if 3a–3d and 3g land: roughly **28,991 → ~15,500** insertions (−2,593 docs, −~2,700 enforcement src+tests, −~230 redirect+fork, −~4,500 test rewrite, −~500 misc consolidation), with production source around 6,500. Honest caveat: 3g's number is the softest; the floor depends on how much coverage the behavior inventory demands.

## 4. Shape of the rewritten branch

**4a** Method: assemble, don't re-implement. New branch from current origin/main; bring over the final tree per area minus the cut list; hand-reconcile only where main moved again (expect drift in `smart_routing.py`-adjacent files — main gained 107 more commits since the last rebase). Every commit is a working slice with its tests.

**4b** Proposed series (~9 commits): (1) routing core — client, seam, arms, substitution, settings; (2) decision persistence + session overrides; (3) server orchestration — turn gates, smart-routing create, pre-session catalogs; (4) claude apply layer; (5) codex apply layer incl. hooks generation + trust; (6) subagent routing — hook scripts, loopback, policy, family rules, override; (7) gateway-inference gating (+ the migration); (8) CLI — flags, `smart_routing_cli`, prompt param, glm route fix; (9) web — all surfaces. Tests ride inside each commit; no separate test commit.

**4c** The docs PR stacks on top (plan, registry, walkthrough, model-state — updated to describe the slimmed scope, with the cut subsystems moved to a "deferred" section).

**4d** The enforcement follow-up PR (3b's content) stacks after merge, restoring canary/watcher/audit/banner with the false-positive fix and the blind-spot decision implemented — by then Bryan has decided the missing-rewrite question.

## 5. CLI fixes integration

**5a** Sequencing rule: the CLI worktree merges into `routing-mvp` FIRST (Bryan's other session owns that merge), the matrix re-runs on the merged tree, and only then does the rewrite assembly start — the rewrite must never race an inbound merge. `907f8886` is already in; the `-p`/`--smart-routing` tiers are pending.

**5b** In the rewrite, CLI lands as commit 8 (4b) — it sits on top of the create-time model-routing extension from tier 2, which itself belongs in commit 3's territory; the assembler must check whether tier 2 changed `_resolve_native_smart_routing` and keep those changes in commit 3, not commit 8.

## 6. Execution and verification

**6a** Fleet plan: one agent per 4b commit-slice working in ONE worktree sequentially per area (the file-ownership partition from the review waves worked; reuse it), a verification agent running the registry recipes after slices 5, 6, 8, and 9, and the lead verifying + pushing. Before anything replaces #3506: full suites, `pre-commit --all-files`, the 15-row matrix + session-start + manual-pin + R9 checks live, registry re-stamped, PR body regenerated from the final diff.

**6b** The old branch survives as `routing-mvp-v1` (like `routing-mvp-backup` before it); the PR either force-pushes or opens fresh — Bryan's call at handoff time.

## 7. Open questions for Bryan's critique

**7a** Is 3b (defer the whole enforcement stack) acceptable, or keep a minimal canary-only slice in-PR? This is the biggest size/functionality trade in the plan.

**7b** Single PR (with 3a docs split) vs the stacked trio (core / gating / enforcement-follow-up)? The series in 4b reads fine either way.

**7c** Does the CLI work stay in this PR (commit 8) or ride its own PR after the core merges? Its server-side tier-2 piece argues for in-PR.

**7d** The test-rewrite target in 3g (py ≤5,500, web ≤1,500) — how hard should the fleet chase it? A softer target (≤7,000 py) halves the risk of coverage loss.

**7e** Anything in 3i Bryan wants cut after all (the chip's raw/applied display, the MODEL_LISTS fork, the layered redirect)?
