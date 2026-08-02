# PR rewrite plan — slimmer, restructured routing PR

> **How to reference this document.** Every block carries an ID: section number + letter (`2c` = third block of §2). Speak the ID and it names the block.

**0a** Goal: a rewritten branch replaces PR #3506. The new branch ships the same three CUJs plus the CLI entry points. It is a fraction of the current size. Its commit series is short enough for a reviewer to read. The three CUJs are Smart Routing as a model choice on Claude Code and Codex, the Smart Routing harness, and routed native subagent spawns. Bryan critiques this plan before the Opus fleet executes it.

**0b** Hard constraints. Bryan chose a complete rewrite from scratch on 2026-08-02 (7g). The fleet therefore writes new code, and it does not move old code. The behavior target does not change, and 2a–2e define it. The branch starts from current origin/main, which is 201 commits ahead again. No evidence transfers: the registry's 15/15 matrix attests to the OLD tree, so every row returns to unverified and the fleet earns it again (6f). Cuts remove scope, not correctness. Only a fully verified branch replaces #3506.

**0c** A from-scratch build must not rediscover what this branch already learned. Three inputs are therefore required reading, per slice, before an agent writes code:

- **The behavior inventory.** `CUJ_STATUS.md` §2 lists every behavior the branch verified. It is the specification for what to build. It is also the coverage gate for the tests (3g).
- **The trap list.** `CUJ_IMPLEMENTATION.md` states each step in the form "why the naive approach failed". `INTELLIGENT_ROUTING_PLAN.md` §12 holds 23 deltas in the form plan assumption → what reality showed → what shipped. Both documents exist because the naive implementation of nearly every step is wrong. An agent that skips them rediscovers each trap by breaking a live session.
- **The reference implementation.** `routing-mvp-v1` (6b) stays checked out in a sibling worktree for the whole build. An agent reads it to answer "what did the working version do here". An agent never copies from it wholesale.

**0d** Rewrite the shape, and transcribe the constants. The structure is worth writing again: the module boundaries, the names, the control flow, and the tests. A small set of values and orderings is NOT worth rediscovering, because experiment produced each one and only the old tree records it. These are examples: `python -I` in the hook command; the codex version probe that runs before config population; the trust handshake that follows the app-server connect and precedes the first turn; the alias-pin exactness check; the timeout ladder; separator-safe prefix stripping. An agent transcribes such a value verbatim, and it cites the trap in a one-line comment. An agent never "cleans up" a constant that it cannot explain.

## 1. What the PR is today

**1a** The PR adds 28,991 insertions over 148 files against origin/main. The composition is: python tests 9,591; web source 5,104; web tests 2,625; docs 2,593; server 2,461; runner+inner 3,054; adapters 1,394; cli+other 1,873; telemetry 296. Production source is ~9,100 lines. Tests are 12,216 lines (42%). Docs are 2,593 lines (9%).

**1b** These are the largest single files: `runner/subagent_routing.py` +1,248; `server/smart_routing.py` +1,120; `NewChatDialog.test.tsx` +956; `orchestration.py` +886; `hook_scripts/subagent_router.py` +675; `cli.py` +505; `NewChatDialog.tsx` +488; `codex_executor.py` +484; `smart_routing_cli.py` +394 (the CLI workstream).

## 2. Keep-core: the minimum each CUJ needs

**2a** CUJ A is the model choice on claude and codex. It keeps:

- `smart_routing.py`: the client, the seam, the arm menus, and the family fallback from 3i.
- The orchestration turn gates.
- Decision persistence as conversation items.
- The claude apply layer: the alias vocabulary, the alias pins, and `/model` injection.
- The codex apply layer: the settings push, the config mirror, and forwarder precedence.
- The chip rendering rules.
- The configure-dialog model option.
- Gateway-backed gating, which is Bryan's explicit rule.

**Managed-plugin readiness is part of this keep-core, not a later port.** The code constructs the routing client once, and it constructs it dynamically. A preview-flag evaluation enables or disables the client at construction time. That evaluation never happens inside `route()` per request. The managed-swap finding gives the reason. A per-request flag evaluation leaves the `routing_client is not None` gates true in a flag-off workspace. The surfaces then offer a pick that the workspace does not serve. When the flag is off, the seam uses LLM-judge routing instead, and the session still runs. The seam does the same when the code cannot construct the external client. The construction seam must match the Databricks managed-plugin shape, so the swap is a plugin registration and not a rewrite.

**2b** CUJ B is the Smart Routing harness. It keeps `_resolve_native_smart_routing`, the pre-session catalogs, `smart_routing_message`, and the harness row with its persistence. It is the only harness that allows cross-family subagents.

**2c** CUJ C is routed subagent spawns. It keeps the hook scripts, the loopback relay, and the server policy (`resolve_subagent_route`). It also keeps the family constraints and the per-session override with its Inherit row. It also keeps the codex `hooks.json` generation, the trust handshake, and `python -I`.

**2d** The CLI is the new workstream, and it must survive the rewrite. It keeps `smart_routing_cli.py`, the `--smart-routing`/`-p` flags, and the glm gateway-route fix (`907f8886`). It also keeps the tier-2/3 commits, which are now merged. `8f3c0c60` (merge `6f2893d9`) is the server half: create-time MODEL routing for a create pinned to one *fixed* native harness. The turn gate can never reach that case, because a TUI's turns originate in the pane. `8d7c9cb2` is the CLI half: the flags, `smart_routing_cli.py`, the dispatch-spec `prompt_param`, and both dispatch tiers. `b10a7239` fixes the `CLAUDE_NATIVE_AGENT_NAME` import against this branch's `harness_plugins` layout. `CUJ_IMPLEMENTATION.md` §6 holds the mechanics. `CUJ_STATUS.md` §2.10 holds the registry rows.

**2e** **The in-session model indicator must show the routed model (new must-fix).** The session UI shows the active model at the bottom right. Bryan saw the terminal run the routed model while that display showed the old one. The rewrite treats this as a bug to fix, not as inherited behavior. Two channels disagree, and that disagreement is the cause: `SessionModelEvent` plus the chatStore picker state on one side, and the pane on the other. The fix belongs in the web commit. The fix makes the display show the same routed value that the pane applied. Three UI surfaces must pass acceptance:

1. Smart Routing appears as a model option on the Claude Code and Codex configure dialogs.
2. Smart Routing appears as a harness.
3. The in-session model display shows the routed model.

## 3. Cut list — each with size, what is lost, and my recommendation

**3a** **Docs → separate PR (−2,593).** The four design docs move to a docs-only PR stacked on the code PR. The move loses no functionality. Recommendation: move them out of the code PR. The docs left the code PR once already, as untracked files. This time they get a second PR instead.

**3b** **The codex enforcement stack → cut entirely (−~1,200 src + ~1,500 tests). RESOLVED (7a).** These parts all leave the tree: the canary, the enforcement watcher, the spawn audit and its reconciliation, the warning banner (web and server halves), `session_warnings`, and the R8 machinery. Bryan's call: make hook execution work all the time, instead of reporting when it does not. A banner that tells the user routing may not have applied is not a product surface. Fix the underlying path instead. So the rewrite ships no canary, no watcher, no spawn audit, no warning banner, and no `session_warnings`. The rewrite keeps the hook generation and the trust handshake, because deterministic subagent routing depends on them. A follow-up may reintroduce observability if hook execution ever proves unreliable in the field. Nothing in this plan schedules that work. This is still the single biggest source-side cut.

**3c** **Cross-harness soft redirect → cut (−~150).** This is the deny message that also says "use `sys_session_send`". It is a product experiment, and the A-sub verification showed that models decline to follow it. The cut loses cross-family *delivery* under auto. The cross-family *decision* still records. Recommendation: cut it. An auto session then constrains spawns to the family of its resolved harness, for v1.

**3d** **Fork-spawn exemption → cut (−~80).** Only tests pin it. Nobody verified it live. It has no user-visible surface. Recommendation: cut it. A fork then inherits the session model implicitly.

**3e** **Telemetry events → keep (±296).** They are small. Review hardened them to family and tier labels only. The managed swap references them. Recommendation: keep them; a cut is not worth the churn.

**3f** **Gateway-inference gating → keep in-PR as its own commit (~900 src+tests).** It is Bryan's explicit product rule. It also holds the PR's only migration. It separates cleanly, so the critique can take it as a stacked PR instead.

**3g** **Test rewrite, not test transplant (−~4,000–5,000 of 12,216).** The current tests grew fix by fix across three review waves. They pin intermediate states. They duplicate coverage across consolidated files. They also carry fixture scaffolding for deleted machinery. The fleet writes a fresh suite against the *final* behavior per area. The coverage-gated method from `f8328623` worked, so reuse it. This time start from the behavior list, not from the old files. **RESOLVED (7d): there is no numeric line target.** The goal is a directed, useful suite that pins the final behavior. The gate is coverage against the registry inventory (`CUJ_STATUS.md` §2), not a line count. Every keep-core behavior gets a test. Nothing gets a test only to reach a number. The suite shrinks because the intermediate-state tests go, and the size that falls out is the size.

**3h** **Web slimming (−~1,000 of 5,104).** 3b takes the banner, its availability plumbing, and their tests out of the tree for good. No banner code and no banner carve-out survives anywhere in the web layer. The dead-code deletions already happened in review. The remaining +488 in `NewChatDialog.tsx` is mostly the harness row, the gating, and the persistence, so keep it. The web commit also carries the 2e model-indicator fix. Recommendation: make no web cuts beyond what 3b implies.

**3i** **Model resolution — RESOLVED (2026-08-01; was "under review").** The research landed, and Bryan ruled. The settled keeps stand (7e): honest `applied=false` records and the raw/applied chip stay, because they caught two real bugs; the session-start cadence machinery stays, because it IS the simple path now. Bryan made three rulings on 2026-08-01:

1. **The resolution machinery reverts to main's simple shape.** The rewrite cuts the `MODEL_LISTS` fork, the cost-substitution machinery (~260 source lines: `MODEL_LISTS`, `_cost_position`, the nearest-cost walk), and the 10-id hardcoded allowlist. Research basis: the substitution path has zero live triggers on the reference workspace; all five frozen arms resolve exactly today; of the 20 recorded raw-model events, 17 were prefix-spelling restores and 3 came from one bug that is already fixed.
2. **Pi is not a routed harness, for now.** 3k holds this ruling and its consequences.
3. **The fallback is one fixed model per family (Bryan's rule; ids settled 2026-08-01).** The claude family falls back to sonnet. Sonnet means the id that the `sonnet` alias pin resolves to (today `databricks-claude-sonnet-5`); the fallback follows the pin, never a hardcoded id. The gpt family and the glm family both fall back to luna (`databricks-gpt-5-6-luna`). Luna is itself a frozen arm, so the router's own menu already contains the fallback target. Luna serves on the codex side, so a glm fallback never leaves its harness. When the router picks an arm the workspace does not serve, the seam applies that family's fallback and stamps `raw_model`, so the record and the chip stay honest. This ruling replaced two earlier drafts: terra is out (the tree knew it only as a pi-exclusion entry), and glm no longer declines — it falls back to luna.

The chain therefore has four steps: strip the prefix → match the catalog exactly → apply the family fallback → decline honestly. An honest decline writes no pin, records `applied=false`, shows a decision card, and keeps the session default model. The session never breaks. Two boundaries hold for the fleet. First, the gateway spelling pin stays: `glm-5-2` serves under the `system.ai.glm-5-2` route, and that pin is a spelling, not a substitution. Second, the claude `/model` alias vocabulary is NOT part of this cut, because the Claude CLI accepts only its own aliases for a mid-session switch; 2a keeps that vocabulary.

**3j** Size target. The old PR's 28,991 insertions are a reference point, and they are no longer a baseline to subtract from, because the fleet writes the new branch rather than trims the old one (7g). The target is a ceiling of about **15,000** insertions, with production source at or below **6,100**. These items are why the number falls:

- The docs move out: about 2,600.
- No enforcement stack: about 2,700 with its tests.
- No redirect and no fork exemption: about 230.
- No resolution machinery and no bar list: about 600 with their tests.
- A directed test suite instead of an accreted one: about 4,500.
- No per-fix consolidation scar tissue: about 500.

Two honest caveats. First, the test line is an estimate and not a target (3g); the coverage gate wins over the ceiling. Second, from-scratch code should land BELOW the assembled estimate, because the fleet never writes the intermediate states that three review waves left behind. The earlier arithmetic gap between this headline and the item list no longer matters, because neither number is a subtraction now.

**3k** **Pi is not a routed harness (RESOLVED 2026-08-01; Bryan: "for now").** Smart-routing eligibility requires a gateway-backed family. That requirement includes the mid-session toggle on ChatPage (`isCostRoutingEligible`). `gateway_inference` reports only the claude and codex families, so pi leaves the routed set. This closes a real hole: a pi session could turn Smart Routing on and pass the gateway rule vacuously, because the rule never saw pi's family. Two consequences follow. The rewrite cuts the bar list (~137 lines plus plumbing), because pi was its only consumer. Our layered-redirect diff against main goes to about zero, because main's wire-compat redirect function stays as main wrote it. The door stays open for later. This PR does no pi work.

**3l** Under the from-scratch method (7g), every entry in this section reads as "do not build this", and not as "delete this". The negative numbers are therefore budget that the fleet does not spend, and they are not deletions from a diff. The reasoning per entry does not change.

## 4. Shape of the rewritten branch

**4a** Method: write the branch from scratch (7g). Start a new branch from current origin/main. Build each slice against the 2a–2e specification and the 0c inputs. Never copy a file wholesale from `routing-mvp-v1`; read it, and then write the new version. One benefit is large. The reconciliation that dominated the last rebase disappears, because the fleet writes against main's current mechanisms from the start: its catalog plumbing, its wire-compat redirect, and its judge rubric. No "our contracts versus main's mechanisms" merge exists any more. Every commit is a working slice with its tests.

**4b** Proposed series (~9 commits). Tests ride inside each commit; there is no separate test commit.

1. Routing core: the client, the seam, the arms, the family fallback, and the settings. This commit also holds **managed-plugin readiness**: the single dynamic client construction, the preview-flag evaluation at construction time, and the graceful fallback to LLM-judge routing when the flag is off or when the code cannot construct the external client. 2a states the rule and the reason.
2. Decision persistence and the session overrides.
3. Server orchestration: the turn gates, the smart-routing create, the pre-session catalogs.
4. The claude apply layer.
5. The codex apply layer, including hooks generation and trust.
6. Subagent routing: the hook scripts, the loopback, the policy, the family rules, the override.
7. Gateway-inference gating, plus the migration.
8. CLI: the flags, `smart_routing_cli`, the prompt param, the glm route fix.
9. Web: all surfaces.

**4c** **RESOLVED (7b): the code ships as one single PR, and the docs split is the only stacking.** The docs PR stacks on top. It holds the plan, the registry, the walkthrough, and the model-state notes, all updated to describe the slimmed scope. It describes the cut subsystems as cut, not as deferred. Gating (3f) stays a commit inside the one PR, not a stacked PR.

**4d** One follow-up PR is planned: **telemetry integration for the Databricks-managed deployment.** The OSS analytics events ship unchanged in this PR (3e). The managed side wires those events into the managed telemetry pipeline after merge. That work happens alongside the managed plugin swap that commit 1 prepares (2a). This block is a placeholder for that work. It is *not* the enforcement follow-up: 3b cuts that stack outright and schedules nothing.

**4e** The from-scratch method moves where the risk sits. An assembled branch carries its integration risk in the merge. A from-scratch branch carries that risk at the end, when nine independently written slices first run together. Two rules contain it. First, slices 1–3 (the routing core, persistence, and orchestration) land and pass their gate BEFORE slices 4–6 start, because every later slice depends on the shape of the seam and of the decision record. Second, no slice enters the series with a failing test (6e). The fleet works in ONE worktree with the file-ownership partition (6a), so a later slice always builds on real code and never on a stub.

## 5. CLI fixes integration

**5a** Sequencing rule: the CLI worktree merges into `routing-mvp` FIRST, and Bryan's other session owns that merge. The matrix re-runs on the merged tree. Only then does the rewrite build start, because the specification branch must stop moving before the fleet writes against it. **Both halves are now in** (`907f8886`, then `8f3c0c60`/`6f2893d9` + `8d7c9cb2` + `b10a7239`), so nothing further is inbound from that worktree. The *verification* is still owed. The CLI surface is unit-verified only, so the re-run must add the `CUJ_STATUS.md` §2.10 rows (recipe **R10**) alongside the 15-row matrix.

**5b** In the rewrite, the CLI is commit 8 (4b). The two CLI commits are **specifications, not patches to apply** (7g). `8f3c0c60` specifies commit 3's server behavior, and `8d7c9cb2` specifies commit 8's CLI behavior. The split is clean, because `8f3c0c60` touches only `orchestration.py` plus its test, and `8d7c9cb2` touches **no** server file. `8d7c9cb2` also did **not** extend `_resolve_native_smart_routing`. The fixed-harness route is a parallel path (`_fixed_native_routing_harness` + `_resolve_fixed_native_model_routing`). One trap must survive the rewrite. Both create paths share `_routing_host_for_create`, and that helper authorizes the host BEFORE it looks the host up. The new code keeps that order, because the reverse order is the §4.3d authorization bug.

## 6. Execution and verification

**6a** Fleet plan: one agent per 4b commit-slice, working in ONE worktree, sequentially per area. The file-ownership partition from the review waves worked, so reuse it. A verification agent runs the registry recipes after slices 5, 6, 8, and 9. The lead verifies and pushes. Before anything replaces #3506, the fleet must complete all of this:

1. The full suites.
2. `pre-commit --all-files`.
3. The 15-row matrix, plus the session-start, manual-pin, and R9 checks, run live.
4. The registry re-stamped.
5. The PR body regenerated from the final diff.

6c defines the evidence bars for these checks. 6d names the recipes. 6e scopes each verification run.

**6b** The old branch survives as `routing-mvp-v1`, like `routing-mvp-backup` before it. Under the from-scratch method it is more than a backup. It is the reference implementation and the behavioral oracle (0c), so the fleet keeps it checked out in a sibling worktree for the whole build. The PR either force-pushes or opens fresh, and Bryan makes that call at handoff time.

**6c** The evidence bars. A behavior counts as verified only when it clears the bar for its layer:

- A routing decision is exact when the raw pick and the applied model name the same arm, and the record shows `applied=true`. A spelling difference is not a substitution.
- Process truth beats UI truth. For claude, the proof is the pane: the status bar shows the routed model, and the transcript holds exactly one `/model` injection per switch. For codex, the proof is the bridge dir: `config.toml` and the newest rollout `turn_context` name the routed model.
- The server log must show zero anomalies for the run: no `harness=None`, no missing-spelling warnings, no malformed router ids.
- The UI acceptance is 2e's three surfaces, and a human checks them on a live stack.
- The fallback and decline steps (3i) have no live trigger on the reference workspace, so unit tests with a synthetic catalog verify them. The live matrix verifies the exact-match path.

**6d** The recipes. `CUJ_STATUS.md` §1 holds the exact commands as reusable handles, and the fleet reuses them instead of inventing new ones: **R0** stack bring-up (the three `run-*.sh` scripts), **R1** the decisions query against the chat DB, **R2** claude pane capture over the runner's tmux socket, **R3** codex `config.toml` + rollout ground truth, **R4** the server-log signature greps, **R5** the router probe, **R7** the headless session driver, **R9** the gateway-gating flip, **R10** the CLI invocations. **R8** (the canary provoke) dies with 3b, so the slim branch's registry drops it. The docs PR updates the registry to the slimmed scope, and the registry stays the source of truth for how to verify every row.

**6e** Per-slice gates. Every slice must pass its own unit tests before it enters the series (4a). The verification agent's four runs (6a) have these scopes:

1. After slice 5, when both apply layers are in: the decision-and-apply matrix rows (A1–A4, B1–B2, C1–C3) via R1 + R2 + R3, with R4 clean.
2. After slice 6, when subagent routing is in: the spawn and toggle rows (B-sub, B-tog, C-sub, C-tog, A-sub), with the family constraints proven in both directions.
3. After slice 8, when the CLI is in: the R10 rows from `CUJ_STATUS.md` §2.10.
4. After slice 9, when the web is in: the three 2e surfaces plus the model-indicator fix, by hand on a live stack, and R9's gating flip in both directions.

The final gate before anything replaces #3506 is 6a's five items, run once on the finished branch.

**6f** Evidence does not transfer. Every ✅ in `CUJ_STATUS.md` attests to the OLD tree, and the new branch inherits none of it. The fleet therefore resets every row to unverified, and it earns each row again. Two consequences follow. The verification tail grows rather than shrinks, and 6e's four runs become the only proof that the new branch works. The registry's own update contract still applies: a status changes only with named evidence, a date, and a commit.

## 7. Bryan's critique — the decisions

**7a** **RESOLVED: cut the enforcement stack entirely; do not defer it with a banner.** Bryan: make it work all the time instead; the warning does not make sense; just fix it. No canary, no watcher, no spawn audit, no warning banner, no `session_warnings`. Hooks generation and the trust handshake stay, because deterministic subagent routing needs them. See 3b, 3h, and overview `2i`.

**7b** **RESOLVED: one single PR.** The docs split (3a) is the only stacking. See 4c.

**7c** **RESOLVED: the CLI stays in this PR**, as commit 8 of the 4b series, as drafted. Its server-side tier-2 piece already sits in commit 3.

**7d** **RESOLVED: no numeric test target.** The fleet writes directed, useful tests that pin the final behavior. Coverage against the registry inventory gates them. The ≤5,500 / ≤1,500 numbers are withdrawn. See 3g.

**7e** **RESOLVED in full (2026-08-01; was "partially resolved").** The settled keeps stand: honest `applied=false` and the raw/applied chip stay. Bryan then ruled on the rest:

1. Cut the `MODEL_LISTS` fork and the cost-substitution table, and revert the resolution machinery to main's shape.
2. Drop pi from the routed set, for now.
3. Use the per-family fallback (claude → sonnet, gpt and glm → luna), with an honest decline behind it.

See 3i and 3k. Bryan closed the last assumptions on 2026-08-01: the gpt and glm families fall back to luna (`databricks-gpt-5-6-luna`, itself a frozen arm); the claude fallback follows the `sonnet` alias pin (today `databricks-claude-sonnet-5`); terra is out. No open assumptions remain.

**7f** Two items entered the plan from the same critique, rather than leaving it. Managed-plugin readiness is a commit-1 requirement (2a, 4b). The in-session model indicator is a must-fix bug (2e).

**7g** **RESOLVED (2026-08-02): a complete rewrite from scratch.** Bryan: keep the code as clean as possible. The fleet writes new code against the 2a–2e specification, and it does not move code from `routing-mvp`. This decision reverses the earlier rule, which said "assemble the branch, do not re-implement it". Five blocks carry the consequences: 0b holds the constraint, 0c holds the required inputs, 0d holds the transcribe rule, 4a holds the method, and 6f holds the evidence reset. The rest of the plan survives the reversal in substance. The cut list still says what not to ship, the 4b commit series is now a build order rather than a slicing order, and the verification plan (6a–6f) is unchanged except that it now carries the whole safety burden.
