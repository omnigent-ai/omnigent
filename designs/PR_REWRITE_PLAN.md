# PR rewrite plan — slimmer, restructured routing PR

> **How to reference this document.** Every block carries an ID: section number + letter (`2c` = third block of §2). Speak the ID and it names the block.

**0a** Goal: a rewritten branch replaces PR #3506. The new branch ships the same three CUJs plus the CLI entry points. It is a fraction of the current size. Its commit series is short enough for a reviewer to read. The three CUJs are Smart Routing as a model choice on Claude Code and Codex, the Smart Routing harness, and routed native subagent spawns. Bryan critiques this plan before the Opus fleet executes it.

**0b** Hard constraints. Bryan chose a complete rewrite from scratch on 2026-08-02 (7g). The fleet therefore writes new code, and it does not move old code. The behavior target does not change, and 2a–2e define it. The branch starts from current origin/main, which is 201 commits ahead again. No evidence transfers: the registry's 15/15 matrix attests to the OLD tree, so every row returns to unverified and the fleet earns it again (6f). Cuts remove scope, not correctness. Only a fully verified branch replaces #3506.

**0c** A from-scratch build must not rediscover what this branch already learned. Three inputs are therefore required reading, per slice, before an agent writes code:

- **The behavior inventory.** `designs/CUJ_STATUS.md` §2 lists every behavior the reference implementation verified, and it is the specification for what to build. It is also the coverage gate for the tests (3g). Read it as a behavior list, not as a status report: its ✅/🟡/⬜ marks and dates describe the OLD tree (6f), and wave 0 removes the rows for work this plan does not build.
- **The trap list.** `designs/CUJ_IMPLEMENTATION.md` states each step in the form "why the naive approach failed". `designs/INTELLIGENT_ROUTING_PLAN.md` §12 holds the deltas in the form plan assumption → what reality showed → what shipped. Both documents exist because the naive implementation of nearly every step is wrong. An agent that skips them rediscovers each trap by breaking a live session. §11 of the same file holds the test matrix and its verbatim prompts, and §1 names the five router arms.
- **The reference implementation.** Branch `routing-mvp-v1`, pinned at `f200a8bd`, is the working version. It stays checked out in a sibling worktree for the whole build. An agent reads it to answer "what did the working version do here". An agent never copies from it wholesale. `designs/ROUTING_OVERVIEW.md` is this plan's companion: it names every subsystem, its files, and its wave.

**0d** Rewrite the shape, and transcribe the constants. The structure is worth writing again: the module boundaries, the names, the control flow, and the tests. A small set of values and orderings is NOT worth rediscovering, because experiment produced each one and only the old tree records it. These are examples: `python -I` in the hook command; the codex version probe that runs before config population; the trust handshake that follows the app-server connect and precedes the first turn; the alias-pin exactness check; the timeout ladder; separator-safe prefix stripping. An agent transcribes such a value verbatim, and it cites the trap in a one-line comment. An agent never "cleans up" a constant that it cannot explain.

## 1. What the PR is today

**1a** PR #3506, the reference implementation on `routing-mvp-v1` (`f200a8bd`), adds **29,924 insertions over 155 files** against origin/main. The composition is: python tests 9,591; web 7,729 (of which `web/package-lock.json` is 3,451 generated lines and `web/src` is 4,278, itself 2,625 tests); docs 3,498 under `designs/` plus `REVIEW_FIXES.md`; server 2,461; runner+inner 3,054; adapters 1,394; cli+other 1,873; telemetry 296. Hand-written production source is about 8,000 lines, and tests are 12,216 lines. Treat every figure here as a scale reference, not as a budget: 3j holds the size rule.

**1b** These are the largest single files: `runner/subagent_routing.py` +1,248; `server/smart_routing.py` +1,120; `NewChatDialog.test.tsx` +956; `orchestration.py` +886; `hook_scripts/subagent_router.py` +675; `cli.py` +505; `NewChatDialog.tsx` +488; `codex_executor.py` +484; `smart_routing_cli.py` +394 (the CLI workstream).

## 2. Keep-core: the minimum each CUJ needs

**2a** CUJ A is the model choice on claude and codex. It keeps:

- `smart_routing.py`: the client, the seam, the arm menus, and the family fallback from 3i.
- The orchestration turn gates.
- Decision persistence as conversation items.
- The claude apply layer: the alias vocabulary, the alias pins, and `/model` injection.
- The codex apply layer: the settings push, the config mirror, forwarder precedence, and the glm gateway route (`907f8886`).
- The chip rendering rules.
- The configure-dialog model option.
- Gateway-backed gating, which is Bryan's explicit rule.

2f holds the routing backend selection that sits behind this seam.

**2b** CUJ B is the Smart Routing harness. It keeps `_resolve_native_smart_routing`, the pre-session catalogs, `smart_routing_message`, and the harness row with its persistence. It is the only harness that allows cross-family subagents, and 3c states how a cross-family need is met. It runs the codex machinery underneath when it resolves to codex, so it inherits the whole codex apply layer, including the glm gateway route.

**2c** CUJ C is routed subagent spawns. It keeps the hook scripts, the loopback relay, and the server policy (`resolve_subagent_route`). It also keeps the family constraints and the per-session override with its Inherit row. It also keeps the codex `hooks.json` generation, the trust handshake, and `python -I`.

**2d** The CLI is the new workstream, and it must survive the rewrite. It keeps `smart_routing_cli.py` and the `--smart-routing`/`-p` flags. It also keeps the tier-2/3 commits, which are now merged. `8f3c0c60` (merge `6f2893d9`) is the server half: create-time MODEL routing for a create pinned to one *fixed* native harness. The turn gate can never reach that case, because a TUI's turns originate in the pane. `8d7c9cb2` is the CLI half: the flags, `smart_routing_cli.py`, the dispatch-spec `prompt_param`, and both dispatch tiers. `b10a7239` fixes the `CLAUDE_NATIVE_AGENT_NAME` import against this branch's `harness_plugins` layout. `CUJ_IMPLEMENTATION.md` §6 holds the mechanics. `CUJ_STATUS.md` §2.10 holds the registry rows.

**2e** **The in-session model indicator must show the routed model (new must-fix).** The session UI shows the active model at the bottom right. Bryan saw the terminal run the routed model while that display showed the old one. The rewrite treats this as a bug to fix, not as inherited behavior. Two channels disagree, and that disagreement is the cause: `SessionModelEvent` plus the chatStore picker state on one side, and the pane on the other. The fix belongs to wave-2 stream 5, and it makes the display show the same routed value that the pane applied. Three UI surfaces must pass acceptance:

1. Smart Routing appears as a model option on the Claude Code and Codex configure dialogs.
2. Smart Routing appears as a harness.
3. The in-session model display shows the routed model.

**2f** **The managed routing backend, chosen per request (7h).** This work ships in this PR. One routing seam holds two backends. A preview-flag evaluation chooses the backend on **every request**, and not once at construction. When the flag is on for the workspace, the seam routes through the AI Gateway `routes:select` API. When the flag is off, the seam routes through the naive LLM judge. A flag-off workspace therefore still gets Smart Routing. The flag selects the routing *quality*, and it never removes the feature. Three consequences follow:

- No surface needs a flag-aware gate. Routing is available either way, so the `routing_client is not None` checks stay correct.
- The two backends offer different menus. task_v1 requires its frozen arm menu, because that menu is a wire contract. The judge scores over the workspace's servable catalog instead. The family fallback (3i) is therefore a task_v1-path concern, because the judge can never pick a model the workspace does not serve.
- The backend choice is independent of gateway-inference gating (3f). The gate asks whether the *pane* runs on the gateway. The flag asks which router picks the model.

The flag itself is managed-side, and OSS must not hardcode it. The Databricks managed plugin evaluates the SAFE flag `databricks.mas.omnigent.intelligentRouting`, which defaults to OFF. OSS therefore exposes a **per-request predicate** on the seam — a callable that the deployment supplies and that the seam consults on each `route()` — and the managed plugin binds that predicate to its flag evaluation. When no deployment supplies a predicate, OSS defaults to "use the AI Gateway client when one is configured, and the judge otherwise". Wave-1 stream 2 builds the predicate seam, the dispatch, and the default. It does not build a flag system.

**2g** **What main already provides.** The fleet writes new code, and it must not re-implement what `origin/main` already ships. `omnigent/server/smart_routing.py` on main (850 lines) already holds `ExternalRoutingClient` (the `routes:select` client), `LLMRoutingClient` (the judge), and `_redirect_incompatible_pick` (the wire-compat redirect that 3k keeps). `omnigent/cli.py` on main already builds one client or the other at startup (`routing_client: ExternalRoutingClient | LLMRoutingClient | None`). Main also owns the discovered-catalog plumbing and the judge rubric. Every stream reads its area on main **before** it writes, and it extends main's mechanism rather than adding a parallel one. This is what shrinks wave-1 stream 2 to a predicate, a dispatch, and a default (2f).

## 3. Cut list — each with size, what is lost, and my recommendation

**3a** **Docs ride the branch, and a final commit deletes them (7j).** Six files ride: `designs/PR_REWRITE_PLAN.md`, `designs/ROUTING_OVERVIEW.md`, `designs/CUJ_STATUS.md`, `designs/CUJ_IMPLEMENTATION.md`, `designs/INTELLIGENT_ROUTING_PLAN.md`, `designs/LIVE_MODEL_STATE.md`, plus `REVIEW_FIXES.md` at the repo root. Wave 0 brings them onto the branch (4b), because none of them exists on `origin/main`. They stay tracked while the PR is open, because Bryan reads them there. They are his reference, and they are not a deliverable. A final commit deletes exactly those paths before the merge, so the merged diff carries **no** docs and no docs PR follows. The other files under `designs/` belong to main; leave them alone.

**3b** **The codex enforcement stack → cut entirely (−~1,200 src + ~1,500 tests). RESOLVED (7a).** These parts all leave the tree: the canary, the enforcement watcher, the spawn audit and its reconciliation, the warning banner (web and server halves), `session_warnings`, and the R8 machinery. Bryan's call: make hook execution work all the time, instead of reporting when it does not. A banner that tells the user routing may not have applied is not a product surface. Fix the underlying path instead. So the rewrite ships no canary, no watcher, no spawn audit, no warning banner, and no `session_warnings`. The rewrite keeps the hook generation and the trust handshake, because deterministic subagent routing depends on them. A follow-up may reintroduce observability if hook execution ever proves unreliable in the field. Nothing in this plan schedules that work. This is still the single biggest source-side cut.

**3c** **The Smart Routing harness agents get the session-creation tool (7i).** Every Smart Routing harness agent gets `sys_session_create`, so an agent that needs another family creates a session instead of failing. A deny message is not a substitute for the tool. The reference implementation tried exactly that, and the `A-sub` row in `CUJ_STATUS.md` records the result: a model reads "spawn denied, use `sys_session_send` instead", and then it gives up. Bryan owns the iteration on how well the agents use it, so the fleet flags the observed behavior for him once the implementation lands, and it does not tune the prompt.

**3d** **Fork-spawn exemption → cut (−~80).** Only tests pin it. Nobody verified it live. It has no user-visible surface. Recommendation: cut it. A fork then inherits the session model implicitly.

**3e** **No telemetry in this PR (7j).** The routing analytics events do not ship here. Bryan takes all telemetry in a follow-up PR (4d), so this PR emits no routing events and adds nothing under `omnigent/telemetry/`.

**3f** **Gateway-inference gating → keep, as a wave-1 workstream (~900 src+tests).** It is Bryan's explicit product rule, and it holds the PR's only migration. The rule has two clauses. First, the Model row offers Smart Routing for a harness only when the host reports that **that harness's** family resolves its inference to the AI Gateway. Second, the Smart Routing harness row appears only when the host reports gateway inference for **both** the claude and the codex families, because the harness routes across both. A host that reports nothing counts as unknown, and unknown never hides the option.

**3g** **Write a directed suite, and do not copy the reference suite (7d).** Start from the behavior inventory in `CUJ_STATUS.md` §2, and write one test per behavior the branch must hold. **No numeric line target governs this.** The gate is coverage against that inventory, and not a line count: every keep-core behavior gets a test, and nothing gets a test only to reach a number. The reference implementation's 12,216 lines of tests are not the model to follow. They grew one fix at a time, so they pin intermediate states that no longer exist, they duplicate coverage across files that were later merged, and they carry fixtures for machinery this plan does not build. The new suite is smaller because it never acquires those, and whatever size falls out of the inventory is the right size.

**3h** **Web slimming.** The reference implementation's `web/src` is 4,278 lines, of which 2,625 are tests, so hand-written web source is about 1,653 lines. Cutting the banner takes a meaningful fraction of that, not a rounding error. 3b takes the banner, its availability plumbing, and their tests out of the tree for good. No banner code and no banner carve-out survives anywhere in the web layer. The dead-code deletions already happened in review. The remaining +488 in `NewChatDialog.tsx` is mostly the harness row, the gating, and the persistence, so keep it. The web commit also carries the 2e model-indicator fix. Recommendation: make no web cuts beyond what 3b implies.

**3i** **Model resolution (7e).** Two things stay: honest `applied=false` records and the raw/applied chip, because they caught two real bugs; and the session-start cadence machinery, because it IS the simple path. Three rules govern the rest:

1. **Build no resolution machinery.** The seam gets no `MODEL_LISTS` table, no cost ordering (`_cost_position`, a nearest-cost walk), and no hardcoded id allowlist. The reference implementation had all three (~260 source lines), and main does not. Evidence for leaving them out: on the reference workspace the substitution path has zero live triggers, all five frozen arms resolve exactly, and of the 20 raw-model events ever recorded, 17 were prefix-spelling restores and 3 came from a single bug.
2. **Pi is not a routed harness, for now.** 3k holds the rule and its consequences.
3. **The fallback is one fixed model per family.** The claude family falls back to sonnet. Sonnet means the id that the `sonnet` alias pin resolves to (today `databricks-claude-sonnet-5`); the fallback follows the pin, never a hardcoded id. The gpt family and the glm family both fall back to luna (`databricks-gpt-5-6-luna`). Luna is itself a frozen arm, so the router's own menu already contains the fallback target. Luna serves on the codex side, so a glm fallback never leaves its harness. When the router picks an arm the workspace does not serve, the seam applies that family's fallback and stamps `raw_model`, so the record and the chip stay honest.

The chain therefore has four steps: strip the prefix → match the catalog exactly → apply the family fallback → decline honestly. An honest decline writes no pin, records `applied=false`, shows a decision card, and keeps the session default model. The session never breaks. Two boundaries hold for the fleet. First, the gateway spelling pin stays: `glm-5-2` serves under the `system.ai.glm-5-2` route, and that pin is a spelling, not a substitution. Second, the claude `/model` alias vocabulary is NOT part of this cut, because the Claude CLI accepts only its own aliases for a mid-session switch; 2a keeps that vocabulary.

**3j** Size. **No line count is a target (Bryan, 2026-08-02).** The goal is a PR that a reviewer can actually read in one sitting, and the whole PR stays as one PR (4c). Shorter is better, and every stream prefers the smaller construction when two work. PR #3506's 28,991 insertions (1a) are the reference point that the fleet must beat by a wide margin, and these items are why the number falls:

- No docs in the merged diff: about 2,900.
- No enforcement stack: about 2,700 with its tests.
- No telemetry: about 300 with its tests.
- No fork exemption: about 80.
- No resolution machinery and no bar list: about 600 with their tests.
- A directed test suite instead of an accreted one: about 4,500.
- No per-fix consolidation scar tissue: about 500.

From-scratch code should land below any estimate that assumes assembly, because the fleet never writes the intermediate states that the reference implementation accumulated. The coverage gate (3g) still wins over any size preference.

**3k** **Pi is not a routed harness, for now (7e).** Smart-routing eligibility requires a gateway-backed family. That requirement includes the mid-session toggle on ChatPage, which main calls `costRoutingEligible` and the reference implementation renamed to `isCostRoutingEligible`. `gateway_inference` reports only the claude and codex families, so pi leaves the routed set. This closes a real hole: a pi session could turn Smart Routing on and pass the gateway rule vacuously, because the rule never saw pi's family. Two consequences follow. The rewrite cuts the bar list (~137 lines plus plumbing), because pi was its only consumer. The branch adds no layered redirect, because main's `_redirect_incompatible_pick` stays as main wrote it (2g). The door stays open for later. This PR does no pi work.

**3l** Under the from-scratch method (7g), every entry in this section reads as "do not build this", and not as "delete this". The negative numbers are therefore budget that the fleet does not spend, and they are not deletions from a diff. The reasoning per entry does not change.

## 4. Shape of the rewritten branch

**4a** Method: write the branch from scratch (7g), and write it in parallel (7k). Start one branch from current origin/main, and keep ONE worktree. Build each workstream against the 2a–2f specification and the 0c inputs. Never copy a file wholesale from `routing-mvp-v1`; read it, and then write the new version. One benefit is large: the reconciliation that dominated the last rebase disappears, because the fleet writes against main's current mechanisms from the start. Two rules make the parallelism safe:

- **Disjoint file ownership.** A workstream owns a file set, and no two concurrent streams own the same file. An agent stages only the files it owns, and it never runs `git add -A`. An agent that meets a failure in a file it does not own reports the failure to the lead, and it does not fix the file. The lead resolves every cross-stream break.
- **The wave-0 contract.** An agent codes against a declared signature, and it never waits for another agent's implementation.

**4b** The build runs as a lead-authored contract commit, two full parallel waves, and a smaller closure wave. Each stream commits its own code with its own tests. A barrier separates the waves, and the lead holds it (4e).

**Wave 0 — the contract (the lead, before wave 1).** One commit, and no wave-1 agent starts before it lands. It does five things:

1. **Declares every shared surface** as a type signature with no logic: the routing-client interface, the backend predicate and dispatch (2f), the decision record, the settings fields, the seam's public functions, the subagent verdict shape, the `gateway_inference` field, and the HTTP create and read-back payloads.
2. **Pre-creates every shared touch point** a wave agent would otherwise have to edit: the settings fields, the route registration, an **empty** alembic revision that wave-1 stream 4 fills, and the two orchestration call sites that wave 2 fills.
3. **Carries the inputs onto the branch.** None of 0c's documents and none of the verification harness exists on `origin/main`. Wave 0 copies the seven doc paths in 3a, plus `LOCAL_SETUP.md`, `dev-env.sh`, `run-server.sh`, `run-host.sh`, `run-frontend.sh`, and `scripts/probe_routing_api.sh`, from `routing-mvp-v1`. Without this step every stream's required reading and both live barriers are unreachable.
4. **Slims the registry.** `designs/CUJ_STATUS.md` still carries rows for the enforcement stack, the warning banner, R8, and telemetry, all of which 3b and 3e do not build. Wave 0 deletes those rows and resets every remaining row to unverified (6f), so waves 1 and 2 are gated on a list that is true.
5. **Writes the barrier-1 apply script** (4e), because that check must exist before wave 1 finishes and no stream owns it.

It also publishes the file partition (4f).

**Wave 1 — foundations (7 streams).** Every stream codes against wave 0, and against nothing else.

1. Routing core and the seam: the client, the arm menus, the resolution chain, and the family fallback.
2. The routing backend selection: the per-request flag evaluation, the AI Gateway path, and the LLM-judge path (2f).
3. Decision persistence: the decision record's writer and reader, and the `model_override` / `harness_override` / `cost_control_mode_override` keys inside the existing `session_overrides` blob. The `subagent_routing_override` key belongs to wave-2 stream 4, and its Inherit row belongs to wave-2 stream 5.
4. The gateway-inference signal: the host-side check, the host frames, the server surface, and the columns inside wave 0's empty migration (3f). The web half of the gate belongs to wave 2, stream 5.
5. The claude apply layer: the alias vocabulary, the alias pins, and `/model` injection. It owns both claude executors, the native one and the SDK one; the claude hook script belongs to wave-2 stream 3.
6. The codex model apply: the settings push, the config mirror, forwarder precedence, and the glm gateway route.
7. Codex `hooks.json` generation and the trust handshake. It owns different files from stream 6, which is why the codex work splits in two: together it is the largest area in the build.

**Wave 2 — integration (6 streams).** Each stream consumes wave 1.

1. The turn gate, in `routing_turn_gate.py`.
2. The create paths, in `routing_create.py`: the Smart Routing harness resolution, the fixed-harness model routing, and the pre-session catalogs. Streams 1 and 2 would collide in `orchestration.py` if they shared it, so each owns a module and orchestration keeps only the call sites that wave 0 declared. That split also produces the cleaner layout that this rewrite exists for.
3. The subagent transport, in `subagent_routing_transport.py` plus the hook scripts: the loopback endpoint, the env plumbing, and the hook subprocess.
4. The subagent policy, in `subagent_routing_policy.py` plus the server relay route: `resolve_subagent_route`, the family constraints, and the per-session override. The reference implementation put the transport and the policy in one 1,248-line file, which two concurrent agents cannot share; the split is deliberate. The policy module is runner-side, and the server route is a thin relay to it.
5. The web surfaces: the dialog, the harness row, the gating consumption, the decision card, and the in-session model-indicator fix (2e).
6. The CLI: the flags, the preflight, and both dispatch tiers.

Streams 5 and 6 sit behind the HTTP boundary, so they consume the wave-0 API contract rather than wave-1 code, and they run in this wave rather than after it. That is what keeps the two largest surfaces off the critical path.

**Wave 3 — closure (3 streams plus the lead).** This wave is smaller and mixed, and it is not a third wave of feature work.

1. `sys_session_create` for the Smart Routing harness agents (3c).
2. The coverage sweep against the registry inventory (3g).
3. The verification agent's barrier-3 runs (6e).

The lead holds barrier-2 fallout — the defects that appear only when the waves run together — and staffs it from whichever streams finished, because it cannot be scoped before barrier 2 exists. The lead then deletes the docs (3a), regenerates the PR body, and runs the full gate (6a).

**4c** **RESOLVED (7b): the code ships as one single PR, and no PR stacks on it.** Gating (3f) is a workstream inside the one PR. The docs ride the same branch and leave it before the merge (3a, 7j), so no docs PR exists.

**4d** One follow-up PR is planned: **telemetry.** No routing telemetry ships in this PR (3e, 7j). Bryan owns the follow-up, and it covers both halves: the OSS analytics events and the wiring into the Databricks-managed telemetry pipeline. That work happens alongside the managed plugin swap that 2f prepares. This block is a placeholder for it. It is *not* the enforcement follow-up: 3b cuts that stack outright and schedules nothing.

**4e** Parallel work concentrates the integration risk at the wave barriers, and the lead holds every barrier. A wave does not start until the previous barrier passes.

**Barrier 1**, after wave 1. Nothing is user-visible yet, so the checks are mechanical and narrow:

1. Every stream's unit tests pass in ONE run, and not only stream by stream.
2. The wave-0 contract file is unchanged. A stream that needs a different signature tells the lead, and the lead re-declares it for everybody.
3. The apply layers work without a router. Wave 0's script pins a hardcoded model onto a claude pane and onto a codex session, and **R2** and **R3** prove it. This de-risks the two hardest layers before any gate exists to reach them through.

**Barrier 2**, after wave 2. The first end-to-end proof: 6e runs 1 to 3.

**Barrier 3**, after wave 3. 6e run 4, and then 6a's five items.

One hazard belongs to the shared worktree, and it is common: one agent's in-progress edit breaks another agent's test collection. Two things contain it. The ownership rule in 4a says report, do not fix. The lead may also serialize two streams that prove to be coupled, and a serialized pair is cheaper than a corrupted barrier.

**4f** The file partition. Wave 0 publishes this table, and 4a's ownership rule refers to it. A stream creates and owns the files on its row; it reads anything else. Paths are for the new branch, and `ROUTING_OVERVIEW.md` §2 maps each subsystem to its reference-implementation files.

| Stream | Owns |
| --- | --- |
| W1·1 routing core | `omnigent/server/smart_routing.py` (extends main's) |
| W1·2 backend | `omnigent/server/routing_backend.py` |
| W1·3 persistence | the decision writer and reader, the session-overrides fields |
| W1·4 gateway signal | `omnigent/gateway_inference.py`, the host frames, the hosts route, wave 0's migration body |
| W1·5 claude apply | `claude_model_vocabulary.py`, `claude_native*.py`, both claude executors |
| W1·6 codex model apply | `codex_native_forwarder.py`, `codex_native_app_server.py`, `codex_native_bridge.py`, `inner/codex_native_executor.py` |
| W1·7 codex hooks and trust | `omnigent/inner/codex_executor.py` |
| W2·1 turn gate | `routing_turn_gate.py` |
| W2·2 create paths | `routing_create.py` |
| W2·3 subagent transport | `subagent_routing_transport.py`, `omnigent/inner/hook_scripts/` |
| W2·4 subagent policy | `subagent_routing_policy.py`, `routes_hooks.py` |
| W2·5 web | `web/src/**` |
| W2·6 CLI | `omnigent/smart_routing_cli.py`, `omnigent/cli_native.py` |
| W3·1 session-create tool | the harness tool exposure |
| W3·2 coverage sweep | test files only, in any area |

`omnigent/cli.py` is **lead-owned for the whole build**, because main already wires the routing client there and both W1·2 and W2·6 need it. A stream sends the lead its `cli.py` change, and the lead applies it. This is a deliberate serialization of the one known hotspot.

## 5. CLI fixes integration

**5a** Every CLI worktree has already merged into `routing-mvp` (`907f8886`, then `8f3c0c60`/`6f2893d9` + `8d7c9cb2` + `b10a7239`), so nothing is inbound and nothing gates the build's start. The *verification* is still owed: the CLI surface is unit-verified only, so a run must add the `CUJ_STATUS.md` §2.10 rows (recipe **R10**).

**5b** In the rewrite, the CLI is a wave-2 workstream, and the create-path work it depends on is a different wave-2 workstream (4b). The two CLI commits are **specifications, not patches to apply** (7g). `8f3c0c60` specifies the server behavior for the create-paths stream, and `8d7c9cb2` specifies the CLI stream. The split is clean, because `8f3c0c60` touches only `orchestration.py` plus its test, and `8d7c9cb2` touches **no** server file. `8d7c9cb2` also did **not** extend `_resolve_native_smart_routing`. The fixed-harness route is a parallel path (`_fixed_native_routing_harness` + `_resolve_fixed_native_model_routing`). One trap must survive the rewrite. Both create paths share `_routing_host_for_create`, and that helper authorizes the host BEFORE it looks the host up. The new code keeps that order, because the reverse order is the authorization bug that `CUJ_IMPLEMENTATION.md` §4.3d describes.

## 6. Execution and verification

**6a** Fleet plan: seven agents in wave 1, six in wave 2, and three in the closure wave, all on one branch and in ONE worktree, per 4b. Strict file ownership (4a, 4f) is what makes concurrent agents safe in one worktree. The verification agent is a standing role from barrier 1 onward; wave-3 stream 3 is that same agent's barrier-3 shift, and not a second one. The lead holds the barriers, verifies, and pushes. Before anything replaces #3506, the fleet must complete all of this:

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
- The UI acceptance is 2e's three surfaces. No agent can close it: Bryan checks them on a live stack, and the fleet's job is to have the stack running and to say what to look at. Treat that as a scheduled handoff at barrier 3, not as a blocker discovered there.
- The fallback and decline steps (3i) have no live trigger on the reference workspace, so unit tests with a synthetic catalog verify them. The live matrix verifies the exact-match path.

**6d** The recipes. `CUJ_STATUS.md` §1 holds the exact commands as reusable handles, and the fleet reuses them instead of inventing new ones: **R0** stack bring-up (the three `run-*.sh` scripts), **R1** the decisions query against the chat DB, **R2** claude pane capture over the runner's tmux socket, **R3** codex `config.toml` + rollout ground truth, **R4** the server-log signature greps, **R5** the UI surface checklist, **R6** the router contract probe (`scripts/probe_routing_api.sh`), **R7** the headless session driver, **R9** the gateway-gating flip, **R10** the CLI routed launch. **R8** (the canary provoke) dies with 3b, so the slim branch's registry drops it. Wave 3 updates the registry to the slimmed scope before the deletion commit removes it (3a), and the registry stays the source of truth for how to verify every row while the PR is open.

**6e** The gates. Every workstream passes its own unit tests before it commits (4a), and the lead re-runs the shared suite after each commit. A red shared suite blocks the next commit. The verification runs sit at the barriers (4e). `CUJ_STATUS.md` §2 is the authority for row names, and `INTELLIGENT_ROUTING_PLAN.md` §11 holds the prompts. One prompt is missing and only Bryan can supply it: §11.1 does not embed **P-SOL**, and rows A3, B2, C2 and two R10 invocations need it. Get it before barrier 2, because §11.1 warns that prompt length changes the router's answer.

1. Barrier 2 — the decision-and-apply matrix rows (A1–A4, B1–B3, C1–C3) via R1 + R2 + R3, with R4 clean.
2. Barrier 2 — the spawn and toggle rows (B-sub, B-tog, C-sub, C-tog, A-sub), with the family constraints proven in both directions.
3. Barrier 2 — the CLI rows (R10), because the CLI lands in wave 2.
4. Barrier 3 — the three 2e surfaces by hand on a live stack, the model-indicator fix, R9's gating flip in both directions, and the flag-off backend: a session in a flag-off workspace still routes, and it routes through the judge (2f).

The final gate before anything replaces #3506 is 6a's five items, run once on the finished branch.

**6f** Evidence does not transfer. Every ✅ in `CUJ_STATUS.md` attests to the OLD tree, and the new branch inherits none of it. The fleet therefore resets every row to unverified, and it earns each row again. Two consequences follow. The verification tail grows rather than shrinks, and 6e's four runs become the only proof that the new branch works. The registry's own update contract still applies: a status changes only with named evidence, a date, and a commit.

## 7. Bryan's critique — the decisions

**7a** **RESOLVED: cut the enforcement stack entirely; do not defer it with a banner.** Bryan: make it work all the time instead; the warning does not make sense; just fix it. No canary, no watcher, no spawn audit, no warning banner, no `session_warnings`. Hooks generation and the trust handshake stay, because deterministic subagent routing needs them. See 3b, 3h, and overview `2i`.

**7b** **RESOLVED: one single PR, and nothing stacks on it.** 7j later removed the docs split, so the docs ride the branch and leave before the merge instead. See 4c and 3a.

**7c** **RESOLVED: the CLI stays in this PR**, as a wave-2 workstream (4b, 5b). The create-path work it depends on is a separate wave-2 workstream, and the HTTP contract from wave 0 lets the two run at the same time.

**7d** **RESOLVED: no numeric test target.** The fleet writes directed, useful tests that pin the final behavior. Coverage against the registry inventory gates them. The ≤5,500 / ≤1,500 numbers are withdrawn. See 3g.

**7e** **RESOLVED in full (2026-08-01; was "partially resolved").** The settled keeps stand: honest `applied=false` and the raw/applied chip stay. Bryan then ruled on the rest:

1. Cut the `MODEL_LISTS` fork and the cost-substitution table, and revert the resolution machinery to main's shape.
2. Drop pi from the routed set, for now.
3. Use the per-family fallback (claude → sonnet, gpt and glm → luna), with an honest decline behind it.

See 3i and 3k. Bryan closed the last assumptions on 2026-08-01: the gpt and glm families fall back to luna (`databricks-gpt-5-6-luna`, itself a frozen arm); the claude fallback follows the `sonnet` alias pin (today `databricks-claude-sonnet-5`); terra is out. No open assumptions remain.

**7f** Two items entered the plan from the same critique, rather than leaving it. Managed-plugin readiness is a build requirement, and 7h moved it out of 2a into 2f. The in-session model indicator is a must-fix bug (2e).

**7g** **RESOLVED (2026-08-02): a complete rewrite from scratch.** Bryan: keep the code as clean as possible. The fleet writes new code against the 2a–2e specification, and it does not move code from `routing-mvp`. This decision reverses the earlier rule, which said "assemble the branch, do not re-implement it". Five blocks carry the consequences: 0b holds the constraint, 0c holds the required inputs, 0d holds the transcribe rule, 4a and 4f hold the method, and 6f holds the evidence reset. The rest of the plan survives the reversal in substance. The cut list still says what not to ship, 4b is now a build order rather than a slicing order, and the verification plan (6a–6f) is unchanged except that it now carries the whole safety burden.

**7h** **RESOLVED (2026-08-02): the managed preview flag is evaluated per request, and not at construction.** Bryan: a workspace without the flag still routes through the naive LLM judge, and a workspace with it routes through the AI Gateway. The work belongs in this PR, and 2f holds the design. One objection to per-request evaluation exists, and it does not apply here: a flag-off workspace would advertise a routing feature that returns no verdict. That cannot happen when the flag-off path routes through the judge.

**7i** **RESOLVED (2026-08-02): keep cross-harness spawning, and give the harness agents `sys_session_create`.** This reverses 3c's cut. An agent that needs another family creates a session, rather than reads a deny message and gives up. Bryan owns the iteration on how well the agents use the tool. The fleet therefore reports the observed behavior after the implementation lands, and it does not tune the prompt.

**7j** **RESOLVED (2026-08-02): telemetry leaves this PR, and the docs ride it without merging.** All routing telemetry moves to a follow-up PR that Bryan owns (3e, 4d). The design documents stay on the branch while the PR is open, because Bryan reads them there, and a final commit deletes them before the merge (3a). The merged diff therefore carries no docs and no telemetry.

**7k** **RESOLVED (2026-08-02): build it in parallel waves.** Bryan set the shape — as parallel as possible, several agents at a time, all on one branch, each in its own workstream — and left the wave count and the verification design to the lead. The plan lands on a wave-0 contract commit, two full waves (7 streams, then 6), and a smaller closure wave (4 streams plus the lead). 4a holds the two safety rules, 4b holds the streams, 4e holds the barriers, and 6a and 6e hold the fleet and gate shapes.
