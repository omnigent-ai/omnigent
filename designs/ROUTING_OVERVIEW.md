# Smart Routing: system map

> **How to reference this document.** Every block carries an ID: the section number plus a letter (`2c` = the third block of §2). Speak the ID and it names the block.

**0a** This document maps the Smart Routing feature at one altitude: the subsystem. It states what each subsystem does, why it must exist, how large it is, and what the rewrite plan does with it. It holds no history and no bug narratives. `designs/CUJ_IMPLEMENTATION.md` holds those. `designs/PR_REWRITE_PLAN.md` holds the cut list that §2 cites by ID.

**0b** Sizes are insertions against `origin/main` at HEAD, from `git diff --numstat`. A size covers the whole file, so a file that serves two subsystems reports the same number in both blocks.

---

## 1. The four user journeys

**1a — Claude Code.** The user opens *Configure Claude Code* and picks **Smart Routing** in the Model row. The server creates the session with routing on and with no model pin. The router scores the first message over the Claude arms. The claude-native executor types `/model` into the pane, and then it injects the message.

**1b — Codex.** The user makes the same pick in *Configure Codex*, over the Codex arms. The codex-native executor sends the routed model to the running thread, and it mirrors the model into the session's `config.toml`. It types nothing into the pane.

**1c — Smart Routing harness.** The user picks the top-level **Smart Routing** row in the harness dropdown. That row is a router over the harnesses, and it is not a harness. The server picks both the harness and the model at session create, from the first message. Both picks stay for the session's life.

**1d — CLI.** The user runs `omnigent claude --smart-routing -p "…"`, or the same flag on `omnigent codex` or `omnigent run`. The CLI checks availability, creates the routed session itself, and then attaches the wrapper to it. A pinned `--harness` routes the model only. No `--harness` routes the harness and the model.

**1e — Subagent spawns.** Every journey above also routes the spawns that the harness makes in-harness. A hook subprocess calls a runner-local endpoint, and the endpoint returns a verdict that rewrites the spawn's model.

---

## 2. The subsystems

**2a — The routing client and the task_v1 contract.** The client calls the AI Gateway `routes:select` API, and it resolves the router's pick to a servable catalog id. `cli.py` builds exactly one client at startup, so no runtime fallback chain exists. Without the client no journey can produce a pick. It lives in `omnigent/server/smart_routing.py` (+1,120 lines, shared with 2b). Fate: keep-core, commit 1 of the series (plan `2a`).

**2b — The resolution seam and arm substitution.** One module boundary holds all router vocabulary: it builds the offered menu, resolves the pick to a `(harness, model)` pair, and substitutes a servable model for an arm the workspace cannot serve. Without the seam every caller would learn the router's vocabulary. It shares `smart_routing.py` with 2a. Fate: keep-core, commit 1. The plan keeps the `MODEL_LISTS` cost table and the layered redirect inside it (plan `3i`).

**2c — The turn and create gates in orchestration.** The server decides per turn whether to route, and it decides per create whether a native session routes its harness, its model, or neither. Without the gates a routed session would re-route on every turn and lose its pin. It lives in `omnigent/server/routes/_sessions/orchestration.py` (+886) and `helpers.py` (+79). Fate: keep-core, commit 3 (plan `2a`, `2b`, `5b`).

**2d — Decision persistence.** The server writes each decision as a conversation item, and it joins the item to the session through two conversation labels. Without persistence no chip renders and no reader can audit a pick. The records add no table and one column. The web reader is `web/src/lib/routingDecision.ts` (+102) plus the server writer inside 2c. Fate: keep-core, commit 2.

**2e — The claude apply layer.** This layer pins the router's arms onto the launch aliases, translates a model id into the pane's own vocabulary, and injects `/model` before the message. Without it a routed pick never reaches the process. It spans `claude_model_vocabulary.py` (+200), `claude_native.py` (+157), `claude_native_bridge.py` (+111), `runner/native/orchestration.py` (+161), and two executors (+123). Fate: keep-core, commit 4 (plan `2a`).

**2f — The codex apply layer.** This layer sends the routed model to the running thread, mirrors it into `config.toml`, and gives the config mirror precedence only when the config changed. Without it the launch default overwrites the routed model after one turn. It spans `codex_native_forwarder.py` (+263), `codex_native_app_server.py` (+271), `codex_native_bridge.py` (+64), and `inner/codex_native_executor.py` (+15). Fate: keep-core, commit 5 (plan `2a`).

**2g — Codex hooks generation and trust.** The code generates the Omnigent half of `hooks.json`, merges it with the user's half in one atomic write, and then runs a trust handshake over the app-server. Without the handshake codex silently skips the routing hooks. It lives in `omnigent/inner/codex_executor.py` (+484). Fate: keep-core, commit 5. The plan keeps this half of the codex machinery even though it defers 2i (plan `2c`, `3b`).

**2h — The subagent loopback, hook scripts, and policy.** A runner-local HTTP endpoint answers the harness's hook subprocess, and the policy returns `allow`, `rewrite`, `redirect`, or `deny`. Without the loopback an in-harness spawn never reaches any router. It spans `runner/subagent_routing.py` (+1,248), `inner/hook_scripts/` (+941), and `routes_hooks.py` (+150). Fate: keep-core, commit 6, minus two cuts: the cross-harness soft redirect (plan `3c`, −~150) and the fork-spawn exemption (plan `3d`, −~80).

**2i — The enforcement and observability stack.** A canary hook writes a file, a watcher reports the file as absent, a spawn audit reconciles the models that actually ran, and a banner shows the warning. Without it codex can skip the hooks and nothing reports the failure. It spans `runtime/session_warnings.py` (+165), `SessionWarningBanner.tsx` (+95), and the watcher and audit code inside 2f and 2g. Fate: deferred to a follow-up PR (plan `3b`, −~1,200 source and −~1,500 tests). This is the largest source-side cut.

**2j — Gateway-inference gating.** The host reports, per harness family, whether its inference resolves to the workspace AI Gateway, and every surface hides Smart Routing on an explicit `false`. Without the gate a host offers a pick that its pane can never run. It spans `gateway_inference.py` (+93), `databricks_ai_gateway.py` (+68), the host frames and store (+192), the hosts routes (+98), one migration (+49), and `smartRoutingAvailability.ts` (+114). Fate: keep in-PR as its own commit 7 (plan `3f`, ~900 source and tests).

**2k — The web surfaces.** The new-chat dialog holds the Model option, the harness row, and the availability notices. The chat page holds the subagent-routing row, and the status blocks render the decision card. Without them the user has no way to pick routing or to read a decision. `web/src` adds +4,278 lines, of which the tests are about 2,625. The largest files are `NewChatDialog.tsx` (+488), `renderItems.ts` (+186), and `ChatPage.tsx` (+180). Fate: keep, minus the banner that 2i takes with it (plan `3h`, −~1,000).

**2l — Telemetry.** Two analytics events record a decision and a setting change, and both reduce a model id to a family label and a tier label. Without them no deployment can measure the routing rate. It lives in `omnigent/telemetry/` (+296). Fate: keep unchanged (plan `3e`).

**2m — The CLI layer.** Three commands take `--smart-routing`, and the CLI runs preflight, drives the create, and passes the routed model to the wrapper as a launch flag. Without it a CLI user must start every routed session in a browser. It spans `cli.py` (+505), `smart_routing_cli.py` (+394), and `cli_native.py` (+77). Fate: keep as commit 8 (plan `2d`, `5b`). §4 holds the open question about its PR placement.

**2n — The tests.** The branch adds +12,278 lines of Python and web tests. They accreted per fix over three review waves, so they pin intermediate states and duplicate coverage. Fate: rewrite against the final behavior rather than transplant (plan `3g`, target −~4,000 to −~5,000).

**2o — The docs.** Five design documents add +2,879 lines under `designs/`, and `REVIEW_FIXES.md` adds +367. They carry the plan, the walkthrough, the evidence registry, and the codex model-state protocol. Fate: move to a separate docs PR stacked on the code PR (plan `3a`, −2,593 as measured at plan time).

---

## 3. The invariants

**3a — Routing runs once per session.** The router runs on the session's first message, and the pick stays for the session's life. The routed turn writes `model_override`, and that pin closes the gate for turn 2. Any cut must keep this cadence.

**3b — `applied` must be honest.** The server writes `applied=false` when the pane cannot apply the pick, and it then writes no pin. A record that claims a model the process never ran is worse than a visible failure.

**3c — A spawn stays in its parent's family.** A child of a Claude session takes a Claude model, and a child of a Codex session takes a Codex model. Only a genuine Smart Routing session may pick across the two families.

**3d — Every gate fails open.** A router outage, a hook timeout, a transport error, or a failed translation leaves the turn unrouted and attaches the reason. Routing is advisory over a system that must work without it.

**3e — The arm menus are a wire contract.** The router version is frozen upstream, so the arm list lives in code and the workspace catalog cannot change it. A menu that the code derives from the catalog returns 400 or scores against an uncalibrated recipe.

**3f — A manual pin blocks routing.** Any `model_override` closes the turn gate, whoever wrote it. The two controls are therefore mutually exclusive in the UI as well.

---

## 4. The open decisions

**4a** Does the whole enforcement stack (2i) defer, or does a canary-only slice stay in the PR? Recommendation: defer the whole stack, because it is observability over an advisory gate (plan `7a`).

**4b** Does the work ship as one PR with the docs split out, or as a stacked trio of core, gating, and enforcement? Recommendation: either reads fine, so pick the one the reviewer prefers (plan `7b`).

**4c** Does the CLI layer (2m) stay in this PR, or ride its own PR after the core merges? Recommendation: keep it in-PR, because its server half already sits in commit 3 (plan `7c`).

**4d** How hard does the fleet chase the test target of 5,500 Python lines and 1,500 web lines? Recommendation: a softer target of 7,000 Python lines halves the risk of coverage loss (plan `7d`).

**4e** Does anything in the explicit keep list get cut after all? The three candidates are the raw-versus-applied arrow on the chip, the `MODEL_LISTS` cost table, and the layered redirect. Recommendation: keep all three (plan `7e`, `3i`).
