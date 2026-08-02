# Smart Routing: system map

> **How to reference this document.** Every block carries an ID: the section number plus a letter (`2c` = the third block of §2). Speak the ID and it names the block.

**0a** This document maps the Smart Routing feature at one altitude: the subsystem. It states what each subsystem does, why it must exist, how large it is, and what the rewrite plan does with it. It holds no history and no bug narratives. `designs/CUJ_IMPLEMENTATION.md` holds those. `designs/PR_REWRITE_PLAN.md` holds the cut list that §2 cites by ID.

**0b** Sizes are insertions against `origin/main` at HEAD, from `git diff --numstat`. A size covers the whole file, so a file that serves two subsystems reports the same number in both blocks.

---

## 1. The four user journeys

**1a — Claude Code.** The user opens _Configure Claude Code_ and picks **Smart Routing** in the Model row. The server creates the session with routing on and with no model pin. The router scores the first message over the Claude arms. The claude-native executor types `/model` into the pane, and then it injects the message.

**1b — Codex.** The user makes the same pick in _Configure Codex_, over the Codex arms. The codex-native executor sends the routed model to the running thread, and it mirrors the model into the session's `config.toml`. It types nothing into the pane.

**1c — Smart Routing harness.** The user picks the top-level **Smart Routing** row in the harness dropdown. That row is a router over the harnesses, and it is not a harness. The server picks both the harness and the model at session create, from the first message. Both picks stay for the session's life. The routed harness set is claude and codex today, and pi is out of it for now (`2j`, `4g`).

**1d — CLI.** The user runs `omnigent claude --smart-routing -p "…"`, or the same flag on `omnigent codex` or `omnigent run`. The CLI checks availability, creates the routed session itself, and then attaches the wrapper to it. A pinned `--harness` routes the model only. No `--harness` routes the harness and the model.

**1e — Subagent spawns.** Every journey above also routes the spawns that the harness makes in-harness. A hook subprocess calls a runner-local endpoint, and the endpoint returns a verdict that rewrites the spawn's model.

---

## 2. The subsystems

**2a — The routing client and the task_v1 contract.** The client calls the AI Gateway `routes:select` API, and it resolves the router's pick to a servable catalog id. `cli.py` builds exactly one client at startup, so no runtime fallback chain exists. Without the client no journey can produce a pick. It lives in `omnigent/server/smart_routing.py` (+1,120 lines, shared with 2b). Fate: keep-core, wave-1 workstream 1 (plan `2a`, `4b`). It is one of two backends behind the seam, and 2p holds the other one and the choice between them.

**2b — The resolution seam and the family fallback.** One module boundary holds all router vocabulary: it builds the offered menu, resolves the pick to a `(harness, model)` pair, and applies the family fallback when the workspace does not serve the picked arm. Without the seam every caller would learn the router's vocabulary. It shares `smart_routing.py` with 2a. Fate: keep-core, wave-1 workstream 1, minus the resolution machinery — **RESOLVED (2026-08-01, plan `3i`)**. We cut the `MODEL_LISTS` fork, the cost-substitution table (~260 source lines), the 10-id allowlist, and the bar list (~137 lines; pi was its only consumer — plan `3k`). The seam reverts to main's simple shape, and our layered-redirect diff against main goes to ~zero. The chain has four steps: strip the prefix → match the catalog exactly → apply the family fallback → decline honestly. The fallback is one fixed model per family: claude → sonnet (the id the `sonnet` alias pin resolves to, today `databricks-claude-sonnet-5`), and gpt and glm → luna (`databricks-gpt-5-6-luna`, itself a frozen arm). A fallback stamps `raw_model`, so the record and the chip stay honest. Research basis: the substitution path has zero live triggers on the reference workspace.

**2c — The turn and create gates in orchestration.** The server decides per turn whether to route, and it decides per create whether a native session routes its harness, its model, or neither. Without the gates a routed session would re-route on every turn and lose its pin. It lives in `omnigent/server/routes/_sessions/orchestration.py` (+886) and `helpers.py` (+79). Fate: keep-core, wave-2 workstreams 1 and 2 — the turn gates and the create paths split into two streams, because they parallelize cleanly (plan `2a`, `2b`, `4b`, `5b`).

**2d — Decision persistence.** The server writes each decision as a conversation item, and it joins the item to the session through two conversation labels. Without persistence no chip renders and no reader can audit a pick. The records add no table and one column. The web reader is `web/src/lib/routingDecision.ts` (+102) plus the server writer inside 2c. Fate: keep-core, wave-1 workstream 3.

**2e — The claude apply layer.** This layer pins the router's arms onto the launch aliases, translates a model id into the pane's own vocabulary, and injects `/model` before the message. Without it a routed pick never reaches the process. It spans `claude_model_vocabulary.py` (+200), `claude_native.py` (+157), `claude_native_bridge.py` (+111), `runner/native/orchestration.py` (+161), and two executors (+123). Fate: keep-core, wave-1 workstream 5 (plan `2a`).

**2f — The codex apply layer.** This layer sends the routed model to the running thread, mirrors it into `config.toml`, and gives the config mirror precedence only when the config changed. Without it the launch default overwrites the routed model after one turn. It spans `codex_native_forwarder.py` (+263), `codex_native_app_server.py` (+271), `codex_native_bridge.py` (+64), and `inner/codex_native_executor.py` (+15). Fate: keep-core, wave-1 workstream 6 (plan `2a`). This stream also owns the glm gateway route — glm serves the Responses API only under its `system.ai.` spelling. The Smart Routing harness runs codex underneath, so it inherits the fix (plan `2b`).

**2g — Codex hooks generation and trust.** The code generates the Omnigent half of `hooks.json`, merges it with the user's half in one atomic write, and then runs a trust handshake over the app-server. Without the handshake codex silently skips the routing hooks. It lives in `omnigent/inner/codex_executor.py` (+484). Fate: keep-core, wave-1 workstream 7 — file-disjoint from 2f, so the codex work runs as two streams. The plan keeps this half of the codex machinery even though it cuts 2i, because deterministic subagent routing depends on it (plan `2c`, `3b`).

**2h — The subagent loopback, hook scripts, and policy.** A runner-local HTTP endpoint answers the harness's hook subprocess, and the policy returns `allow`, `rewrite`, `redirect`, or `deny`. Without the loopback an in-harness spawn never reaches any router. It spans `runner/subagent_routing.py` (+1,248), `inner/hook_scripts/` (+941), and `routes_hooks.py` (+150). Fate: keep-core, split into wave-2 workstreams 3 and 4 — the transport and the policy, which own separate files and therefore parallelize. One cut remains: the fork-spawn exemption (plan `3d`, −~80). Cross-harness spawning stays (plan `3c`, `4j`): a Smart Routing harness agent gets `sys_session_create`, so it creates a session for another family instead of reading a deny message. Wave-3 workstream 1 adds the tool.

**2i — The enforcement and observability stack.** A canary hook writes a file, a watcher reports the file as absent, a spawn audit reconciles the models that actually ran, and a banner shows the warning. Without it codex can skip the hooks and nothing reports the failure. It spans `runtime/session_warnings.py` (+165), `SessionWarningBanner.tsx` (+95), and the watcher and audit code inside 2f and 2g. Fate: **cut entirely** (plan `3b`, `7a`, −~1,200 source and −~1,500 tests). Bryan's call: make the hooks run every time instead of reporting when they do not, because a warning banner is not a product answer. The rewrite therefore ships no canary, no watcher, no spawn audit, no banner, and no `session_warnings`. The plan schedules no follow-up. A future PR may reintroduce observability only if hook execution proves unreliable in the field. This is still the largest source-side cut.

**2j — Gateway-inference gating.** The host reports, per harness family, whether its inference resolves to the workspace AI Gateway, and every surface hides Smart Routing on an explicit `false`. Without the gate a host offers a pick that its pane can never run. It spans `gateway_inference.py` (+93), `databricks_ai_gateway.py` (+68), the host frames and store (+192), the hosts routes (+98), one migration (+49), and `smartRoutingAvailability.ts` (+114). Fate: keep in-PR as wave-1 workstream 4 (plan `3f`, ~900 source and tests). The rule has two clauses (Bryan, 2026-08-02). The Model row offers Smart Routing for a harness only when the host reports gateway inference for *that harness's* family. The Smart Routing harness row appears only when the host reports gateway inference for *both* families, because the harness routes across both. A host that reports nothing counts as unknown, and unknown never hides the option. The gate also fixes the routed harness set: eligibility, including the mid-session toggle, requires a gateway-backed family, and pi has none, so pi is not a routed harness for now (plan `3k`, resolved 2026-08-01).

**2k — The web surfaces.** The new-chat dialog holds the Model option, the harness row, and the availability notices. The chat page holds the subagent-routing row, and the status blocks render the decision card. Without them the user has no way to pick routing or to read a decision. `web/src` adds +4,278 lines, of which the tests are about 2,625. The largest files are `NewChatDialog.tsx` (+488), `renderItems.ts` (+186), and `ChatPage.tsx` (+180). Fate: keep as wave-2 workstream 5, minus the banner that 2i takes with it, and with no banner carve-out left behind (plan `3h`, −~1,000). It sits behind the HTTP boundary, so it codes against the wave-0 contract and runs beside the server work rather than after it. This layer also owns a **known bug the rewrite must fix**: the in-session model indicator at the bottom right of the session UI must show the routed model. Bryan saw the terminal run the routed model while that display still showed the old one. The `SessionModelEvent` and chatStore picker-state channel disagrees with the pane, and the fix makes the display read the value the pane applied (plan `2e`). The three UI surfaces that must pass acceptance are: Smart Routing as a model option on the Claude Code and Codex configure dialogs, Smart Routing as a harness, and a correct in-session model display.

**2l — Telemetry.** Two analytics events record a decision and a setting change, and both reduce a model id to a family label and a tier label. Without them no deployment can measure the routing rate. It lives in `omnigent/telemetry/` (+296). Fate: **cut from this PR** (plan `3e`, `4k`, resolved 2026-08-02). Bryan takes all telemetry in a follow-up PR that he owns, and it covers both the OSS events and the Databricks-managed pipeline wiring (plan `4d`). This PR emits no routing events.

**2m — The CLI layer.** Three commands take `--smart-routing`, and the CLI runs preflight, drives the create, and passes the routed model to the wrapper as a launch flag. Without it a CLI user must start every routed session in a browser. It spans `cli.py` (+505), `smart_routing_cli.py` (+394), and `cli_native.py` (+77). Fate: keep as wave-2 workstream 6, inside the single PR (plan `2d`, `5b`, `4b`). It sits behind the HTTP boundary, so it runs beside the create-path stream it depends on.

**2n — The tests.** The branch adds +12,278 lines of Python and web tests. They accreted per fix over three review waves, so they pin intermediate states and duplicate coverage. Fate: rewrite against the final behavior rather than transplant (plan `3g`). No line target governs the rewrite. The gate is coverage against the registry inventory, and the suite is as large as directed, useful tests of the final behavior require.

**2o — The docs.** Five design documents add +2,879 lines under `designs/`, and `REVIEW_FIXES.md` adds +367. They carry the plan, the walkthrough, the evidence registry, and the codex model-state protocol. Fate: **ride the branch, then leave it** (plan `3a`, `4k`, resolved 2026-08-02). They stay tracked while the PR is open, because Bryan reads them there. A final commit deletes them before the merge, so the merged diff carries no docs and no docs PR follows.

**2p — The routing backend selection.** A preview-flag evaluation runs on **every request** and chooses which router answers: the AI Gateway `routes:select` API (2a) when the flag is on, or the naive LLM judge when it is off. Without it a managed deployment cannot ship Smart Routing behind a flag, and a flag-off workspace would lose the feature instead of degrading it. Fate: build it in this PR as wave-1 workstream 2 (plan `2f`, `7h`, resolved 2026-08-02). Three consequences: no surface needs a flag-aware gate, because routing exists either way; the two backends offer different menus, since task_v1 needs its frozen arms as a wire contract while the judge scores over the servable catalog; and the flag is independent of 2j, because 2j asks whether the *pane* runs on the gateway while the flag asks which router picks the model.

---

## 3. The invariants

**3a — Routing runs once per session.** The router runs on the session's first message, and the pick stays for the session's life. The routed turn writes `model_override`, and that pin closes the gate for turn 2. Any cut must keep this cadence.

**3b —** `applied` **must be honest.** The server writes `applied=false` when the pane cannot apply the pick, and it then writes no pin. A record that claims a model the process never ran is worse than a visible failure.

**3c — A spawn stays in its parent's family.** A child of a Claude session takes a Claude model, and a child of a Codex session takes a Codex model. Only a genuine Smart Routing session may pick across the two families.

**3d — Every gate fails open.** A router outage, a hook timeout, a transport error, or a failed translation leaves the turn unrouted and attaches the reason. Routing is advisory over a system that must work without it.

**3e — The arm menus are a wire contract on the task_v1 path.** The router version is frozen upstream, so the arm list lives in code and the workspace catalog cannot change it. The judge backend (2p) has no such contract, and it scores over the servable catalog instead. A menu that the code derives from the catalog returns 400 or scores against an uncalibrated recipe. The machinery that resolves an arm to a servable model is now settled (plan `3i`): exact match, then the family fallback, then an honest decline. The wire contract itself does not change.

**3f — A manual pin blocks routing.** Any `model_override` closes the turn gate, whoever wrote it. The two controls are therefore mutually exclusive in the UI as well.

**3g — The display agrees with the pane.** The in-session model indicator shows the model the process is running. If the pane applied a routed model, the indicator shows that model. A display that disagrees with the pane teaches the user to distrust the feature, so the picker-state channel follows the applied value and never the stale pick (plan `2e`).

**3h — An unservable pick falls back inside the family, or it declines.** When the workspace does not serve the router's pick, the seam applies the family's designated fallback (claude → sonnet, gpt and glm → luna) and stamps `raw_model`. When the workspace does not serve the fallback either, the seam declines honestly and the session keeps its default model. The session never breaks (plan `3i`).

---

## 4. The decisions

**4a — RESOLVED: the enforcement stack (2i) is cut, not deferred.** Bryan's call: make the hooks run every time instead of warning that they may not have. No canary, no watcher, no spawn audit, no banner. Hooks generation and the trust handshake (2g) stay (plan `7a`, `3b`).

**4b — RESOLVED: one single PR, and nothing stacks on it.** Gating (2j) is a workstream inside that PR, and the docs (2o) ride the branch and leave before the merge (plan `7b`, `4k`).

**4c — RESOLVED: the CLI layer (2m) stays in this PR**, as a wave-2 workstream that runs beside the create-path stream it depends on (plan `7c`, `5b`).

**4d — RESOLVED: no numeric test target.** The fleet writes directed tests of the final behavior and gates them on coverage against the registry inventory. The 5,500 and 1,500 line numbers are withdrawn (plan `7d`, `3g`).

**4e — RESOLVED (2026-08-01).** The honest `applied=false` record and the raw-versus-applied arrow on the chip stay. Bryan ruled on the rest: cut the `MODEL_LISTS` fork and the cost table; revert the seam to main's shape; use the per-family fallback (claude → sonnet, gpt and glm → luna) with an honest decline behind it (plan `7e`, `3i`). Bryan closed the last assumptions on 2026-08-01: gpt and glm fall back to luna (`databricks-gpt-5-6-luna`, itself a frozen arm); the claude fallback follows the `sonnet` alias pin. No open assumptions remain.

**4f — Two items the same critique added.** Managed-plugin readiness is a commit-1 requirement (2a), and the in-session model indicator is a must-fix bug (2k, 3g).

**4g — RESOLVED: pi is not a routed harness for now.** Smart-routing eligibility requires a gateway-backed family, and the requirement includes the mid-session toggle. The bar list goes with it, and the layered-redirect diff against main goes to ~zero (plan `3k`).

**4h — RESOLVED (2026-08-02): the fleet writes the new branch from scratch.** Bryan: keep the code as clean as possible. This reverses the earlier "assemble, do not re-implement" rule. Nothing in §2's fates changes, and the §3 invariants still hold. Three plan blocks carry the risk that the method adds: `0c` names the inputs an agent reads before it writes a slice (the behavior inventory, the trap list, and the reference implementation on `routing-mvp-v1`), `0d` says to rewrite the shape but transcribe the empirical constants, and `6f` records that no evidence transfers, so the fleet earns every registry row again.

**4i — RESOLVED (2026-08-02): the managed preview flag is evaluated per request.** A flag-off workspace still gets Smart Routing through the naive LLM judge, and a flag-on workspace gets AI Gateway routing. The work ships in this PR, in its own block (2p, plan `2f`, `7h`). This reverses the managed-swap report's construction-time recommendation.

**4j — RESOLVED (2026-08-02): cross-harness spawning stays, with a real affordance.** Every Smart Routing harness agent gets `sys_session_create`, so it creates a session for another family rather than reading a deny message. Bryan owns the iteration on how well the agents use it, and the fleet only reports the behavior it observes (2h, plan `3c`, `7i`).

**4k — RESOLVED (2026-08-02): telemetry leaves, and the docs ride without merging.** All routing telemetry moves to a follow-up PR that Bryan owns (2l). The design documents stay on the branch while the PR is open and a final commit deletes them before the merge (2o, plan `7j`).

**4l — RESOLVED (2026-08-02): build it in parallel waves.** Bryan set the shape and left the wave count to the lead: a wave-0 contract commit, then 7 streams, then 6, then a closure wave of 4. Every stream owns a disjoint file set, and the contract commit declares every shared signature first, so no stream waits on another stream's implementation. The lead holds a barrier between the waves (plan `4a`, `4b`, `4e`, `7k`).
