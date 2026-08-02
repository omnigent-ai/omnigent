# PR rewrite plan — slimmer, restructured routing PR

> **How to reference this document.** Every block carries an ID: section number + letter (`2c` = third block of §2). Speak the ID and it names the block.

**0a** Goal: replace PR #3506 with a rewritten branch. The new branch ships the same three CUJs plus the CLI entry points at a fraction of the current size, in a commit series a reviewer can read. The three CUJs are: Smart Routing as a model choice on Claude Code and Codex; the Smart Routing harness; routed native subagent spawns. Bryan critiques this plan before the Opus fleet executes it.

**0b** Hard constraints. The rewrite starts from the current verified tree, not from scratch. Every behavior on the branch is evidence-verified (registry: 15/15 matrix, live checks), and a re-implementation would lose that evidence. Cuts remove scope, not correctness. The final branch rebases onto current main (we are 201 commits behind again), and it re-runs the §11 matrix before it replaces #3506.

## 1. What the PR is today

**1a** 28,991 insertions / 148 files against origin/main. Composition: python tests 9,591; web source 5,104; web tests 2,625; docs 2,593; server 2,461; runner+inner 3,054; adapters 1,394; cli+other 1,873; telemetry 296. Production source is ~9,100 lines. Tests are 12,216 lines (42%). Docs are 2,593 lines (9%).

**1b** Largest single files: `runner/subagent_routing.py` +1,248; `server/smart_routing.py` +1,120; `NewChatDialog.test.tsx` +956; `orchestration.py` +886; `hook_scripts/subagent_router.py` +675; `cli.py` +505; `NewChatDialog.tsx` +488; `codex_executor.py` +484; `smart_routing_cli.py` +394 (the CLI workstream).

## 2. Keep-core: the minimum each CUJ needs

**2a** CUJ A (model choice on claude/codex) keeps: `smart_routing.py` (the client, the seam, the arm menus, the family fallback from 3i); the orchestration turn gates; decision persistence as conversation items; the claude apply layer (alias vocabulary, alias pins, `/model` injection); the codex apply layer (settings push, config mirror, forwarder precedence); the chip rendering rules; the configure-dialog model option; gateway-backed gating (Bryan's explicit rule). **Managed-plugin readiness is part of this keep-core, not a later port.** We construct the routing client once, and we construct it dynamically. A preview-flag evaluation enables or disables the client at construction time — never inside `route()` per request. The managed-swap finding gives the reason: a per-request flag evaluation leaves the `routing_client is not None` gates true in a flag-off workspace, so the surfaces offer a pick that the workspace does not serve. When the flag is off, or when we cannot construct the external client, the seam falls back to LLM-judge routing and the session still runs. The construction seam must match the Databricks managed-plugin shape, so the swap is a plugin registration and not a rewrite.

**2b** CUJ B (Smart Routing harness) keeps: `_resolve_native_smart_routing`, the pre-session catalogs, `smart_routing_message`, the harness row and its persistence, and cross-family subagents (allowed only under this harness).

**2c** CUJ C (routed subagent spawns) keeps: the hook scripts, the loopback relay, the server policy (`resolve_subagent_route`), the family constraints, the per-session override with the Inherit row, and the codex `hooks.json` generation + trust handshake + `python -I`.

**2d** CLI (the new workstream, and it must survive the rewrite): `smart_routing_cli.py`, the `--smart-routing`/`-p` flags, the glm gateway-route fix (`907f8886`), and the tier-2/3 commits, which are now merged. `8f3c0c60` (merge `6f2893d9`) is the server half: create-time MODEL routing for a create pinned to one *fixed* native harness. The turn gate can never reach that case, because a TUI's turns originate in the pane. `8d7c9cb2` is the CLI half: the flags, `smart_routing_cli.py`, the dispatch-spec `prompt_param`, and both dispatch tiers. `b10a7239` fixes the `CLAUDE_NATIVE_AGENT_NAME` import against this branch's `harness_plugins` layout. Mechanics: `CUJ_IMPLEMENTATION.md` §6. Registry rows: `CUJ_STATUS.md` §2.10.

**2e** **The in-session model indicator must show the routed model (new must-fix).** The session UI shows the active model at the bottom right. Bryan saw the terminal run the routed model while that display showed the old one. The rewrite treats this as a bug to fix, not as inherited behavior. The cause is a disagreement between two channels: `SessionModelEvent` plus the chatStore picker state on one side, and the pane on the other. The fix belongs in the web commit, and it makes the display show the same routed value that the pane applied. Three UI surfaces must pass acceptance: (a) Smart Routing appears as a model option on the Claude Code and Codex configure dialogs; (b) Smart Routing appears as a harness; (c) the in-session model display shows the routed model.

## 3. Cut list — each with size, what is lost, and my recommendation

**3a** **Docs → separate PR (−2,593).** The four design docs move to a docs-only PR stacked on the code PR. Zero functionality lost. Recommendation: move them out of the code PR. (We untracked them once already; this time they get a second PR, not untracked files.)

**3b** **The codex enforcement stack → cut entirely (−~1,200 src + ~1,500 tests). RESOLVED (7a).** The canary, the enforcement watcher, the spawn audit and its reconciliation, the warning banner (web and server halves), `session_warnings`, and the R8 machinery all leave the tree. Bryan's call: make hook execution work all the time instead of reporting when it does not. A banner that tells the user routing may not have applied is not a product surface — fix the underlying path. So the rewrite ships no canary, no watcher, no spawn audit, no warning banner, and no `session_warnings`. We keep the hook generation and the trust handshake, because deterministic subagent routing depends on them. A follow-up may reintroduce observability if hook execution ever proves unreliable in the field; nothing in this plan schedules that work. This is still the single biggest source-side cut.

**3c** **Cross-harness soft redirect → cut (−~150).** This is the deny-plus-"use `sys_session_send`" message. It is a product experiment, and the A-sub verification showed that models decline to follow it. Lost: cross-family *delivery* under auto (the cross-family *decision* still records). Recommendation: cut it; an auto session constrains spawns to the resolved harness's family for v1.

**3d** **Fork-spawn exemption → cut (−~80).** It is test-pinned only, never verified live, and it has no user-visible surface. Recommendation: cut it; a fork inherits the session model implicitly.

**3e** **Telemetry events → keep (±296).** They are small, review-hardened (family and tier labels only), and the managed swap references them. Recommendation: keep them; a cut is not worth the churn.

**3f** **Gateway-inference gating → keep in-PR as its own commit (~900 src+tests).** It is Bryan's explicit product rule, and it carries the PR's only migration. It is cleanly separable if the critique wants a stacked PR instead.

**3g** **Test rewrite, not test transplant (−~4,000–5,000 of 12,216).** The current tests grew fix by fix across three review waves: they pin intermediate states, they duplicate coverage across consolidated files, and they carry fixture scaffolding for deleted machinery. The fleet writes a fresh suite against the *final* behavior per area (the coverage-gated method from `f8328623` worked; this time start from the behavior list, not from the old files). **RESOLVED (7d): there is no numeric line target.** The goal is a directed, useful suite that pins the final behavior. The gate is coverage against the registry inventory (`CUJ_STATUS.md` §2), not a line count. Every keep-core behavior gets a test. Nothing gets a test only to reach a number. The suite shrinks because the intermediate-state tests go, and the size that falls out is the size.

**3h** **Web slimming (−~1,000 of 5,104).** 3b takes the banner, its availability plumbing, and their tests out of the tree for good, so no banner code and no banner carve-out survives anywhere in the web layer. The dead-code deletions already happened in review. `NewChatDialog.tsx`'s remaining +488 is mostly the harness row, the gating, and the persistence — keep it. The web commit also carries the 2e model-indicator fix. Recommendation: no web cuts beyond what 3b implies.

**3i** **Model resolution — RESOLVED (2026-08-01; was "under review").** The research landed, and Bryan ruled on all three questions. The machinery reverts to main's simple shape. The settled keeps stand (7e): honest `applied=false` records and the raw/applied chip stay — they caught two real bugs; the session-start cadence machinery stays, because it IS the simple path now. The three rulings:

- **Cut the resolution machinery.** We cut the `MODEL_LISTS` fork, the cost-substitution machinery (~260 source lines: `MODEL_LISTS`, `_cost_position`, the nearest-cost walk), and the 10-id hardcoded allowlist. Research basis: the substitution path has zero live triggers on the reference workspace; all five frozen arms resolve exactly today; of the 20 recorded raw-model events, 17 were prefix-spelling restores and 3 came from one bug that is already fixed.
- **The chain becomes four steps.** Strip the prefix → match the catalog exactly → apply the family fallback → decline honestly. An honest decline writes no pin, records `applied=false`, shows a decision card, and keeps the session default. The session never breaks.
- **The fallback is one fixed model per family (Bryan's rule).** The claude family falls back to sonnet. The gpt family falls back to terra (catalog id `databricks-gpt-5-6-terra`, confirmed in `LIVE_MODEL_STATE.md`). A fallback stamps `raw_model`, so the record and the chip stay honest. Assumption for Bryan to confirm: glm is a single-arm family with no designated fallback, so an unservable glm pick declines honestly.

Two boundaries for the fleet: the gateway spelling pin (glm serves under the `system.ai.` route) stays, and it is a spelling, not a substitution; the claude `/model` alias vocabulary is NOT part of this cut, because the Claude CLI accepts only its aliases for a mid-session switch (2a keeps it).

**3j** Net size estimate if 3a–3d, 3g, and the 3i/3k cuts land: roughly **28,991 → ~14,900** insertions (−2,593 docs, −~2,700 enforcement src+tests, −~230 redirect+fork, −~600 resolution machinery + bar list with their tests, −~4,500 test rewrite, −~500 misc consolidation), with production source around 6,100. Honest caveat: the test line is an estimate, not a target (3g). The floor depends on how much coverage the behavior inventory demands, and the inventory wins.

**3k** **Pi is not a routed harness (RESOLVED 2026-08-01; Bryan: "for now").** Smart-routing eligibility requires a gateway-backed family, and that requirement includes the mid-session toggle on ChatPage (`isCostRoutingEligible`). `gateway_inference` reports only the claude and codex families, so pi leaves the routed set. This closes a real hole: a pi session could toggle Smart Routing on and pass the gateway rule vacuously, because the rule never saw pi's family. Consequences: we cut the bar list (~137 lines plus plumbing) — pi was its only consumer; our layered-redirect diff against main goes to ~zero, because main's wire-compat redirect function stays as main wrote it. The door stays open for later. This PR does no pi work.

## 4. Shape of the rewritten branch

**4a** Method: assemble, do not re-implement. Start a new branch from current origin/main. Bring over the final tree per area, minus the cut list. Hand-reconcile only where main moved again (expect drift in the `smart_routing.py`-adjacent files — main gained 107 more commits since the last rebase). Every commit is a working slice with its tests.

**4b** Proposed series (~9 commits). Tests ride inside each commit; there is no separate test commit.

1. Routing core: the client, the seam, the arms, the family fallback, the settings — **plus managed-plugin readiness**: the single dynamic client construction, the preview-flag evaluation at construction time, and the graceful fallback to LLM-judge routing when the flag is off or when the external client cannot be built (2a states the rule and the reason).
2. Decision persistence and the session overrides.
3. Server orchestration: the turn gates, the smart-routing create, the pre-session catalogs.
4. The claude apply layer.
5. The codex apply layer, including hooks generation and trust.
6. Subagent routing: the hook scripts, the loopback, the policy, the family rules, the override.
7. Gateway-inference gating, plus the migration.
8. CLI: the flags, `smart_routing_cli`, the prompt param, the glm route fix.
9. Web: all surfaces.

**4c** **RESOLVED (7b): the code ships as one single PR, and the docs split is the only stacking.** The docs PR stacks on top (the plan, the registry, the walkthrough, the model-state notes — updated to describe the slimmed scope, with the cut subsystems described as cut, not deferred). Gating (3f) stays a commit inside the one PR, not a stacked PR.

**4d** One follow-up PR is planned: **telemetry integration for the Databricks-managed deployment.** The OSS analytics events ship unchanged in this PR (3e). The managed side — wiring those events into the managed telemetry pipeline — lands after merge, alongside the managed plugin swap that commit 1 prepares (2a). This block is a placeholder for that work. It is *not* the enforcement follow-up: 3b cuts that stack outright and schedules nothing.

## 5. CLI fixes integration

**5a** Sequencing rule: the CLI worktree merges into `routing-mvp` FIRST (Bryan's other session owns that merge). The matrix re-runs on the merged tree. Only then does the rewrite assembly start — the rewrite must never race an inbound merge. **Both halves are now in** (`907f8886`, then `8f3c0c60`/`6f2893d9` + `8d7c9cb2` + `b10a7239`), so nothing further is inbound from that worktree. The *verification* is still owed: the CLI surface is unit-verified only, so the re-run must add the `CUJ_STATUS.md` §2.10 rows (recipe **R10**) alongside the 15-row matrix.

**5b** In the rewrite, CLI lands as commit 8 (4b) — and the tier-2 server half is already a **separate** commit, so the split is mechanical rather than a hand-untangling job. `8f3c0c60` touches only `orchestration.py` plus its test, and it goes into commit 3 verbatim. `8d7c9cb2` touches **no** server file, and it goes into commit 8 verbatim. It also did **not** extend `_resolve_native_smart_routing`: the fixed-harness route is a parallel path (`_fixed_native_routing_harness` + `_resolve_fixed_native_model_routing`), and the only edit to the auto path is a refactor that lifts its authorize-first host lookup into the shared `_routing_host_for_create`. The assembler must keep that helper, because both create paths now call it, and a drop would undo the §4.3d authorization-order fix.

## 6. Execution and verification

**6a** Fleet plan: one agent per 4b commit-slice, working in ONE worktree, sequentially per area (the file-ownership partition from the review waves worked; reuse it). A verification agent runs the registry recipes after slices 5, 6, 8, and 9. The lead verifies and pushes. Before anything replaces #3506: the full suites, `pre-commit --all-files`, the 15-row matrix + session-start + manual-pin + R9 checks live, the registry re-stamped, and the PR body regenerated from the final diff.

**6b** The old branch survives as `routing-mvp-v1` (like `routing-mvp-backup` before it). The PR either force-pushes or opens fresh — Bryan's call at handoff time.

## 7. Bryan's critique — the decisions

**7a** **RESOLVED: cut the enforcement stack entirely; do not defer it with a banner.** Bryan: make it work all the time instead; the warning does not make sense; just fix it. No canary, no watcher, no spawn audit, no warning banner, no `session_warnings`. Hooks generation and the trust handshake stay, because deterministic subagent routing needs them. See 3b, 3h, and overview `2i`.

**7b** **RESOLVED: one single PR.** The docs split (3a) is the only stacking. See 4c.

**7c** **RESOLVED: the CLI stays in this PR**, as commit 8 of the 4b series, as drafted. Its server-side tier-2 piece already sits in commit 3.

**7d** **RESOLVED: no numeric test target.** The fleet writes directed, useful tests that pin the final behavior, gated on coverage against the registry inventory. The ≤5,500 / ≤1,500 numbers are withdrawn. See 3g.

**7e** **RESOLVED in full (2026-08-01; was "partially resolved").** The settled keeps stand: honest `applied=false` and the raw/applied chip stay. Bryan ruled on the rest: cut the `MODEL_LISTS` fork and the cost-substitution table; revert the resolution machinery to main's shape; use the per-family fallback (claude → sonnet, gpt → terra) with an honest decline behind it; drop pi from the routed set for now. See 3i and 3k. Two assumptions stay open for Bryan's veto: glm has no fallback and declines; terra's catalog id is `databricks-gpt-5-6-terra`.

**7f** Two items entered the plan from the same critique rather than leaving it: managed-plugin readiness as a commit-1 requirement (2a, 4b) and the in-session model indicator as a must-fix bug (2e).
