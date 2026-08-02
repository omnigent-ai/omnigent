# PR rewrite plan — slimmer, restructured routing PR

> **How to reference this document.** Every block carries an ID: section number + letter (`2c` = third block of §2). Speak the ID and it names the block.

**0a** Goal: replace PR #3506 with a rewritten branch that ships the same three CUJs (Smart Routing as a model choice on Claude Code and Codex; the Smart Routing harness; routed native subagent spawns) plus the CLI entry points, at a fraction of the current size, in a commit series a reviewer can actually read. This document is the plan Bryan critiques before the Opus fleet executes it.

**0b** Hard constraints. The rewrite starts from the current verified tree, not from scratch — every behavior on the branch is evidence-verified (registry: 15/15 matrix, live checks) and re-implementation would forfeit that. Cuts remove *scope*, not correctness. The final branch rebases onto current main (we are 201 commits behind again) and re-runs the §11 matrix before it replaces #3506.

## 1. What the PR is today

**1a** 28,991 insertions / 148 files against origin/main. Composition: python tests 9,591; web source 5,104; web tests 2,625; docs 2,593; server 2,461; runner+inner 3,054; adapters 1,394; cli+other 1,873; telemetry 296. Production source is ~9,100 lines; tests are 12,216 (42%); docs 2,593 (9%).

**1b** Largest single files: `runner/subagent_routing.py` +1,248; `server/smart_routing.py` +1,120; `NewChatDialog.test.tsx` +956; `orchestration.py` +886; `hook_scripts/subagent_router.py` +675; `cli.py` +505; `NewChatDialog.tsx` +488; `codex_executor.py` +484; `smart_routing_cli.py` +394 (the CLI workstream).

## 2. Keep-core: the minimum each CUJ needs

**2a** CUJ A (model choice on claude/codex): `smart_routing.py` (client, seam, arm menus, substitution), the orchestration turn gates, decision persistence as conversation items, the claude apply layer (vocabulary, alias pins, /model inject), the codex apply layer (settings push, config mirror, forwarder precedence), the chip rendering rules, the configure-dialog model option, gateway-backed gating (Bryan's explicit rule). **Managed-plugin readiness is part of this keep-core, not a later port.** The routing client is constructed ONCE, dynamically, and a preview-flag evaluation enables or disables it. The flag is evaluated at enable time, when the client is built — never inside `route()` per request. The managed-swap finding is the reason: a per-request flag evaluation leaves the `routing_client is not None` gates true in a flag-off workspace, so the surfaces still offer a pick the workspace will not serve. When the flag is off, or when the external client cannot be built, the seam falls back to LLM-judge routing and the session still runs. The construction seam must match the Databricks managed plugin shape, so the swap is a plugin registration and not a rewrite.

**2b** CUJ B (Smart Routing harness): `_resolve_native_smart_routing`, pre-session catalogs, `smart_routing_message`, harness row + persistence, cross-family subagents allowed only here.

**2c** CUJ C (routed subagent spawns): hook scripts + loopback relay + server policy (`resolve_subagent_route`), family constraints, per-session override with the Inherit row, codex hooks.json generation + trust handshake + `python -I`.

**2d** CLI (the new workstream, must survive the rewrite): `smart_routing_cli.py`, the `--smart-routing`/`-p` flags, the glm gateway-route fix (`907f8886`), and the tier-2/3 commits, which have now merged: `8f3c0c60` (merge `6f2893d9`) is the server half — create-time MODEL routing for a create pinned to one *fixed* native harness, which the turn gate can never reach because a TUI's turns originate in the pane; `8d7c9cb2` is the CLI half (flags, `smart_routing_cli.py`, the dispatch-spec `prompt_param`, both dispatch tiers); `b10a7239` fixes the `CLAUDE_NATIVE_AGENT_NAME` import against this branch's `harness_plugins` layout. Mechanics: `CUJ_IMPLEMENTATION.md` §6; registry rows: `CUJ_STATUS.md` §2.10.

**2e** **The in-session model indicator must show the routed model (new must-fix).** The session UI displays the active model at the bottom right. Bryan observed the terminal running the routed model while that display showed the old one, so the rewrite treats it as a bug to fix, not as inherited behavior. The cause is a disagreement between two channels: `SessionModelEvent` and the chatStore picker state on one side, the pane on the other. The fix belongs in the web commit, and it makes the display read the same routed value the pane applied. The three UI surfaces that must pass acceptance are: (a) Smart Routing offered as a model option on the Claude Code and Codex configure dialogs, (b) Smart Routing offered as a harness, and (c) the in-session model display showing the routed model.

## 3. Cut list — each with size, what is lost, and my recommendation

**3a** **Docs → separate PR (−2,593).** The four design docs move to a docs-only PR stacked on the code PR. Zero functionality lost. Recommendation: cut from the code PR. (We already did this dance once; this time it is a second PR, not untracked files.)

**3b** **The codex enforcement stack → cut entirely (−~1,200 src + ~1,500 tests). RESOLVED (7a).** The canary, the enforcement watcher, the spawn audit + reconciliation, the warning banner (web + server halves), `session_warnings`, and the R8 machinery all leave the tree. Bryan's call: make hook execution work all the time instead of reporting when it does not. A banner that tells the user routing may not have applied does not make sense as a product surface — fix the underlying path. So the rewrite ships no canary, no watcher, no spawn audit, no warning banner, and no `session_warnings`. Keep the hook generation + trust handshake, because deterministic subagent routing depends on them. A follow-up may reintroduce observability if hook execution ever proves unreliable in the field; nothing in the plan schedules that work. This remains the single biggest source-side cut.

**3c** **Cross-harness soft redirect → cut (−~150).** The deny-plus-"use sys_session_send" message. It is a product experiment; the A-sub verification showed models decline to follow it. Lost: any cross-family *delivery* under auto (the cross-family *decision* still records). Recommendation: cut; auto sessions constrain spawns to the resolved harness's family for v1.

**3d** **Fork-spawn exemption → cut (−~80).** Test-pinned only, never verified live, no user-visible surface. Recommendation: cut; forks inherit the session model implicitly.

**3e** **Telemetry events → keep (±296).** Small, review-hardened (family/tier labels only), and the managed swap references them. Recommendation: keep; not worth the churn to cut.

**3f** **Gateway-inference gating → keep in-PR but as its own commit (~900 src+tests).** Bryan's explicit product rule; also the PR's only migration. It is cleanly separable if the critique wants it as a stacked PR instead.

**3g** **Test rewrite, not test transplant (−~4,000–5,000 of 12,216).** The current tests accreted per-fix across three review waves: they pin intermediate states, duplicate coverage across consolidated files, and carry fixture scaffolding for deleted machinery. The fleet writes a fresh test suite against the *final* behavior per area (the coverage-gated method from `f8328623` worked; this time start from the behavior list, not the old files). **RESOLVED (7d): there is no numeric line target.** The goal is a directed, useful suite that pins the final behavior. The gate is coverage against the registry inventory (`CUJ_STATUS.md` §2), not a line count. Every keep-core behavior gets a test; nothing gets a test only to reach a number. The suite shrinks because the intermediate-state tests go, and the size that falls out is the size.

**3h** **Web slimming (−~1,000 of 5,104).** 3b takes the banner, its availability plumbing, and their tests out of the tree for good, so no banner code and no banner carve-out survives anywhere in the web layer. The dead-code deletions already happened in review. `NewChatDialog.tsx`'s remaining +488 is mostly the harness row + gating + persistence — keep. The web commit also carries the 2e model-indicator fix. Recommendation: no further web cuts beyond what 3b implies.

**3i** **Things explicitly NOT cut, and one group still under review.** Settled keeps (7e, partially resolved): honest `applied=false` records and the raw/applied chip stay — they caught two real bugs; session-start cadence machinery stays, because it IS the simple path now. **Under review — research in flight:** the `MODEL_LISTS` fork, the cost-substitution table, and the layered redirect. These three are the model-resolution machinery, and they are pending Bryan's review rather than settled. Bryan's directive for that review: with an AIGW-backed host and a configured client there should be no deep fallback chain at all. Never break the session — but prefer an honest decline over a silent substitution driven by a cost table the user cannot see. The final call lands after the research does, and the fleet does not lock this area in before then.

**3j** Net size estimate if 3a–3d and 3g land: roughly **28,991 → ~15,500** insertions (−2,593 docs, −~2,700 enforcement src+tests now cut outright, −~230 redirect+fork, −~4,500 test rewrite, −~500 misc consolidation), with production source around 6,500. Honest caveat: the test line is an estimate, not a target (3g). The floor depends on how much coverage the behavior inventory demands, and the inventory wins.

## 4. Shape of the rewritten branch

**4a** Method: assemble, don't re-implement. New branch from current origin/main; bring over the final tree per area minus the cut list; hand-reconcile only where main moved again (expect drift in `smart_routing.py`-adjacent files — main gained 107 more commits since the last rebase). Every commit is a working slice with its tests.

**4b** Proposed series (~9 commits): (1) routing core — client, seam, arms, substitution, settings, **plus managed-plugin readiness**: the single dynamic client construction, the preview-flag evaluation at enable time, and the graceful fallback to LLM-judge routing when the flag is off or the external client cannot be built (2a states the rule and the reason); (2) decision persistence + session overrides; (3) server orchestration — turn gates, smart-routing create, pre-session catalogs; (4) claude apply layer; (5) codex apply layer incl. hooks generation + trust; (6) subagent routing — hook scripts, loopback, policy, family rules, override; (7) gateway-inference gating (+ the migration); (8) CLI — flags, `smart_routing_cli`, prompt param, glm route fix; (9) web — all surfaces. Tests ride inside each commit; no separate test commit.

**4c** **RESOLVED (7b): the code ships as one single PR, and the docs split is the only stacking.** The docs PR stacks on top (plan, registry, walkthrough, model-state — updated to describe the slimmed scope, with the cut subsystems described as cut, not deferred). Gating (3f) stays a commit inside the one PR, not a stacked PR.

**4d** One follow-up PR is planned, and it is **telemetry integration for the Databricks-managed deployment**. The OSS analytics events ship unchanged in this PR (3e); the managed side — wiring those events into the managed telemetry pipeline — lands after merge, alongside the managed plugin swap that commit 1 prepares for (2a). This block is a placeholder for that work. It is *not* the enforcement follow-up: 3b cuts that stack outright and schedules nothing.

## 5. CLI fixes integration

**5a** Sequencing rule: the CLI worktree merges into `routing-mvp` FIRST (Bryan's other session owns that merge), the matrix re-runs on the merged tree, and only then does the rewrite assembly start — the rewrite must never race an inbound merge. **Both halves are now in** (`907f8886`, then `8f3c0c60`/`6f2893d9` + `8d7c9cb2` + `b10a7239`), so nothing further is inbound from that worktree. What is still owed is the *verification*: the CLI surface is unit-verified only, so the re-run must add the `CUJ_STATUS.md` §2.10 rows (recipe **R10**) alongside the 15-row matrix.

**5b** In the rewrite, CLI lands as commit 8 (4b) — and the tier-2 server half is already a **separate** commit, so the split is mechanical rather than a hand-untangling job. `8f3c0c60` touches only `orchestration.py` plus its test and goes into commit 3 verbatim; `8d7c9cb2` touches **no** server file and goes into commit 8 verbatim. It also did **not** extend `_resolve_native_smart_routing`: the fixed-harness route is a parallel path (`_fixed_native_routing_harness` + `_resolve_fixed_native_model_routing`), and the only edit to the auto path is a refactor that lifts its authorize-first host lookup into the shared `_routing_host_for_create` — which the assembler must keep, because both create paths now call it and dropping it un-does the §4.3d authorization-order fix.

## 6. Execution and verification

**6a** Fleet plan: one agent per 4b commit-slice working in ONE worktree sequentially per area (the file-ownership partition from the review waves worked; reuse it), a verification agent running the registry recipes after slices 5, 6, 8, and 9, and the lead verifying + pushing. Before anything replaces #3506: full suites, `pre-commit --all-files`, the 15-row matrix + session-start + manual-pin + R9 checks live, registry re-stamped, PR body regenerated from the final diff.

**6b** The old branch survives as `routing-mvp-v1` (like `routing-mvp-backup` before it); the PR either force-pushes or opens fresh — Bryan's call at handoff time.

## 7. Bryan's critique — the decisions

**7a** **RESOLVED: cut the enforcement stack entirely, do not defer it with a banner.** Bryan: make it work all the time instead; the warning does not make sense; just fix it. No canary, no watcher, no spawn audit, no warning banner, no `session_warnings`. Hooks generation and the trust handshake stay, because deterministic subagent routing needs them. See 3b, 3h, and overview `2i`.

**7b** **RESOLVED: one single PR.** The docs split (3a) is the only stacking. See 4c.

**7c** **RESOLVED: the CLI stays in this PR**, as commit 8 of the 4b series, as drafted. Its server-side tier-2 piece already sits in commit 3.

**7d** **RESOLVED: no numeric test target.** The fleet writes directed, useful tests that pin the final behavior, gated on coverage against the registry inventory. The ≤5,500 / ≤1,500 numbers are withdrawn. See 3g.

**7e** **Partially resolved.** Honest `applied=false` and the raw/applied chip stay — decided. The rest of 3i (the `MODEL_LISTS` fork, the cost-substitution table, the layered redirect) is pending Bryan's review with research in flight; see 3i for the directive that governs the call.

**7f** Two items entered the plan from the same critique rather than leaving it: managed-plugin readiness as a commit-1 requirement (2a, 4b) and the in-session model indicator as a must-fix bug (2e).
