# Intelligent Routing × AIGW: MVP Engineering Plan (Bryan's workstreams)

Owner: bryan.qiu@databricks.com
Status: ACTIVE — updated 2026-07-29 against main @ `c62bfc2f`. Router API behavior
confirmed by live probes (§1); backend read from universe `ai-gateway/src/routing`
(§1.1); **every omnigent-side claim below verified against code** (file:line
refs are exact as of `c62bfc2f`). MVP target: **Jul 31, 2026**.
Branch strategy: **all MVP work lands on the single branch `routing-mvp`**
(worktree `~/omnigent-routing-mvp`, cut from latest main). No per-task branches —
tasks are partitioned by file ownership (§4) so parallel agents don't collide.
Split into reviewable PRs only after end-to-end testing on this branch.

MVP requirements (from the Jul 28 brainstorm/meeting):

1. When using AIGW for inference, intelligent routing goes to the AIGW routing API.
2. Subagent routing is deterministic — including subagents spawned *natively* by
   Claude Code (Task tool) and Codex (`spawn_agent`), not just Omnigent-spawned
   child sessions.
3. All routing decisions are visible in the UI.
4. Telemetry: capture when intelligent routing is enabled, when users switch OFF
   of it mid-flight, and forks off an intelligent-routing session (OTel).
5. SAFE flag with isaac to default-on for specific users (lives in universe, not
   this repo — tracked in §8, not a task packet here).

---

## 1. Confirmed router API behavior (live probes, 2026-07-28/29)

Probed `POST {workspace}/ai-gateway/routing/v1/routes:select` on
**eng-ml-inference** and **eng-ml-agent-platform** staging (bearer via
`databricks auth token`). Two routers are registered: `task_v0` and `task_v1`
(the error for an unknown name enumerates them). **`task_v1` is the
harness-aware recipe — use it.** Response shape for both:
`{"route_selection": [{"route_option": {"model", "harness"}, "params": {}}], "rationale": "..."}`.

**`task_v1` (target):**

- **Scenario inference from model arms, not harness tags.** The router infers
  a scenario from *which model families appear* in `route_options`:
  Claude arms `{claude-opus-4-8, claude-sonnet-5}` → scenario `cc`;
  Codex arms `{glm-5-2, gpt-5-6-sol, gpt-5-6-luna}` → scenario `codex`;
  both families → scenario `both`. Offering no recognized arm (e.g. only
  `gpt-5-5`) → 400 "could not infer a scenario".
- **Each scenario requires its full fixed menu** (`cc` = both Claude arms,
  `codex` = all three Codex arms, `both` = all five). A partial menu → 400
  naming the missing arms. **Extra non-arm models are tolerated and ignored**
  (verified: menu + `gpt-5-5`/`claude-haiku-4-5`/`kimi-k2` routes fine) — so
  omnigent can keep sending a catalog superset as long as the full menu for
  the intended scenario is present.
- **This arm-menu selection IS the harness constraint**: send only Codex arms
  for P0 (within-codex), only Claude arms for P1 (within-CC), all five for
  the auto/cross-harness CUJ. The pick always comes from the offered menu.
- **The `harness` field itself is still passthrough**, on both routers:
  swapping tags (Claude models tagged `codex` and vice versa) neither changes
  the pick nor errors — the tag is echoed back verbatim on the selection,
  even when nonsensical. Harness intent is expressed via which arms you
  offer, not via the tag; treat the echoed harness as untrusted.
- Recipe is a rule tree (rationales expose predicates: `prompt<300`,
  `not_crosscutting`, `low_ambiguity`, "cheapest arm … never escalate";
  defaults `claude-sonnet-5` / `gpt-5-6-sol`).

**`task_v0` (previous placeholder, still registered):** static required set
`[claude-opus-4-8, glm-5-2, gpt-5-4-mini, gpt-5-5, gpt-5-6-luna]`, no
tolerance for missing ids, no harness behavior at all. Ignore except for
backward-compat testing.

**Menu ids need not exist as serving endpoints.** eng-ml-inference has
endpoints for `claude-opus-4-8`, `claude-sonnet-5`, `glm-5-2`, but **not**
`gpt-5-6-sol` / `gpt-5-6-luna` — yet task_v1 requires those ids in
`route_options` and happily *selects* them. Consequences: (a) a strictly
catalog-derived option list 400s (the two missing arms never enter it), so the
client must inject the router vocabulary; (b) a pick may be unservable in the
workspace and must be mapped to a servable id (or the decision degraded to
no-routing) — see §2.

### 1.1 Backend implementation notes (universe `ai-gateway/src/routing/`)

Read the server source; it confirms the probes and adds contract facts the
client design should exploit:

- **Versioned routers are frozen.** A `router_name` pin means "same decision
  forever"; behavior changes ship as `task_v2`, never edits to v1
  (`router/CLAUDE.md`, the FROZEN banner). So scenario menus can only drift
  when *we* change `routing.router_name` — pinning the name pins the menu,
  which de-fangs risk #4 (drift is opt-in, not ambient).
- **Every task_v1 call makes an LLM extraction self-call under the caller's
  identity** (three axes: `expected_change_scope`, `prompt_ambiguity`,
  `difficulty`) — even Rule-0 needs `llm difficulty == easy`, so there is no
  LLM-free fast path. Extraction model = `route_selector.config.model` if
  set, else the frozen default `gpt-5-4-mini`, resolved as
  `system.ai.<model>` in the caller's workspace. Two implications:
  (a) routing latency ≈ one small-model call — real, budget it on the spawn
  path (risk #2); (b) **the caller needs query access to the extraction
  model** — P1 should pass `routing.selection_model` through as
  `route_selector.config.model` so deployments can pin one they have.
- **`task.prompt` is the entire routing signal.** Deterministic features are
  regexes/lengths over the prompt (stack traces, file paths, `` `symbols` ``,
  code fences; buckets at 400/1200/3000 chars; trivial cutoff 300). Send the
  user's raw task text, not a wrapper/summary — wrapping changes routing.
  (Client already truncates to 4000 chars — `smart_routing.py:505` — which
  preserves the "long" bucket boundary at 3000; keep the truncation.)
  Corollary for P2: Codex's encrypted spawn prompts mean hook-path codex
  routing degenerates to short-prompt defaults; the redirect path
  (plaintext) is where task-aware quality lives.
- **`harness` is never read by any router.** `RouteOption.harness` is
  documented "optional for a native harness, required for a metaharness";
  selection matches on model only ("first harness wins on a duplicate"), and
  post-validation just checks the picked option was offered verbatim.
  Confirms: derive harness client-side from the picked arm's family.
- **Malformed model ids are silently dropped** from `route_options`
  (normalizer: lowercase, `.`→`-`), not 400ed — catalog junk is safe to send.
- **`session_history` exists in the request schema but no shipped router
  reads it** — the designed hook for per-turn / "sidekick" routing (open
  questions 6/10). The client should be shaped to populate it later (P2's
  decision cache already retains per-session picks).
- **`routes:select` does no access or existence checks** on offered options —
  explains unservable arms being required *and* selectable; resolution is
  entirely ours (§2).
- **No server-side decision logging yet** (TODO in `RoutingHandler`) — until
  AIGW's background-activity log lands, omnigent's decision items + OTel (P5)
  are the *only* record of routing decisions. Raises the stakes on P5.
- The wire types in universe are temporary "until Omnigent's routing protos
  sync" — `omnigent/api/routing/v1/routing.proto` is the source of truth, so
  contract extensions (codebase metadata, fork context, spawn-eligible sets)
  start as PRs on *our* proto. Endpoint is SAFE-gated server-side
  (`routeSelectionEnabled`).

Dev-loop config (worktree `.omnigent-local/config.yaml`, isolated via
`OMNIGENT_CONFIG_HOME`/`OMNIGENT_DATA_DIR`): routing `provider: external`,
`base_url: https://eng-ml-inference.staging.cloud.databricks.com/ai-gateway/routing/v1`,
`router_name: task_v1`, profile `eng-ml-inference`. Note
`OMNIGENT_SMART_ROUTING=1` from the Jul 24 runbook is obsolete — the client is
built from the `routing:` config block alone (`cli.py::_build_external_routing_client`).

## 2. Design principle: one swappable route-options boundary

Today's contract (fixed per-scenario arm menus, harness expressed by arm
choice rather than a first-class field, picks that may not be servable) is
temporary. Mason will later accept dynamic model+harness sets and enforce
harness constraints server-side. To make that a config/adapter change rather
than a refactor, **all knowledge of the router's contract lives in exactly one
place**: a `RouteOptionSource` seam inside `omnigent/server/smart_routing.py`:

- `build_route_options(harnesses, catalog) -> list[RouteOption]` — v0
  implementation selects the task_v1 scenario menu from the requesting
  harness set (codex-only → Codex arms, claude-only → Claude arms, mixed →
  all five; menus config-overridable via `routing.scenario_menus`), injecting
  menu ids even when absent from the catalog. A future
  `CatalogRouteOptionSource` returns the caller's real model set.
- `resolve_selection(pick, harnesses, catalog) -> ResolvedRoute` — v0 ignores
  the router's echoed `harness` (passthrough, untrusted — §1), derives the
  harness from the picked arm's family (keeping
  `_HARNESS_EXCLUDED_MODELS`-style compatibility data here rather than
  deleting it), and maps router vocabulary → servable catalog id (prefix
  restore + nearest-available fallback when the picked id has no endpoint,
  e.g. `gpt-5-6-sol`/`gpt-5-6-luna` on eng-ml-inference today). When
  server-side constraints ship, this collapses to prefix restore only.

Callers (`route_session_harness`, `route_turn`, the P2 subagent endpoint)
never see router vocabulary or harness-correction rules. Router recipe name
stays a config key (`routing.router_name`), never hardcoded in logic.

**Failure semantics (corrected):** the "external vs LLM judge" choice is
**build-time** — `cli.py:3420` constructs exactly one client into
`RuntimeCaps.routing_client`; there is no runtime chain. At runtime a router
failure returns `None` with `last_error` set (`smart_routing.py:518-582`),
and callers proceed **unrouted** with the reason surfaced
(`route_session_harness` returns the error string for the UI). MVP keeps
this fail-open posture for sessions/turns; the P2 subagent path adds the
configurable strict mode (§5.1) because "unrouted spawn" there means the
determinism guarantee silently lapses.

## 3. State of the world on main (verified 2026-07-29, `c62bfc2f`)

| Capability | Where (exact) |
|---|---|
| AIGW routing client: proto-typed request (`routing_pb2` + `json_format`, snake_case), per-call Databricks OAuth refresh off-thread, 4000-char prompt cap, `last_error` surfacing | `omnigent/server/smart_routing.py:378-587` |
| Routing proto — **already has** `RouteSelector.config` (Struct), `SessionHistory`, `RouteSelection.params`; client just doesn't populate them | `omnigent/api/routing/v1/routing.proto` |
| Local LLM-judge router (built INSTEAD of external when `provider != external`) | `smart_routing.py:250`; `cli.py:107,166,3420` |
| Pluggable router | `omnigent/runtime/caps.py:80` (`routing_client`) |
| Auto-harness session routing | `smart_routing.py:678` ← `orchestration.py:3625` |
| Omnigent-spawned child routing via parent catalog | `orchestration.py:3717` (`catalog_session_id=parent`) |
| Session-start routing (`cost_control_mode_override == "on"`; native `/model` injection) — fires on the first message, then the routed pick is pinned as `model_override` and later turns reuse it | `smart_routing.py:823` ← `orchestration.py:3748, 3995-4012` |
| Post-verdict harness correction | `smart_routing.py:636-675` (`_HARNESS_EXCLUDED_MODELS`, `_redirect_incompatible_pick`) |
| Live catalog fetch (server→runner `/v1/sessions/{id}/models`) | `smart_routing.py:122` (`fetch_runner_models`) |
| Deterministic model-override plumbing (native `--model`, SDK `HARNESS_<H>_MODEL`, `model_family_mismatch` guard) | `omnigent/model_override.py:30,112,254` |
| `sys_session_send` child model: **create-time-only** `args.model`, family-guarded, → `model_override` | `omnigent/tools/builtins/spawn.py:1562-1662,1778-1781` |
| Routing-decision transcript item — fields today: `model`, `applied`, `rationale`, `agent` | `omnigent/entities/conversation.py:512-560` |
| UI decision rendering: `RoutingDecisionChip` (turn) + `RoutingDecisionCard` (session) | `web/src/components/blocks/StatusBlocks.tsx:130,172` |
| UI Smart Routing sentinel, hard-disabled for Auto harness | `HarnessConfigControls.tsx:17`, `NewChatDialog.tsx:2165` |
| Claude-native hook provisioning — settings dict built in code, passed via `--settings`; **a deny-capable PreToolUse policy hook already exists** (AskUserQuestion matcher + policy eval when `ap_server_url` set) | `claude_native_bridge.py:1118-1364` (`build_hook_settings`), `:1322-1325` |
| Runner-local HTTP endpoint pattern for hooks: tool relay on `127.0.0.1:0`, bearer token advertised via `tool_relay.json` in the bridge dir, discovery env vars | `claude_native_bridge.py:3291,3317,3326`; env `HARNESS_CLAUDE_NATIVE_BRIDGE_DIR` / `..._REQUEST_SESSION_ID` (`:804-805`) |
| Codex per-session private home: `auth.json` symlinked, `hooks.json` **symlinked from the user's** `~/.codex`, `config.toml` copied | `omnigent/inner/codex_executor.py:112-120,656,760,1380` |
| Codex hook-trust groundwork **already present**: `_CODEX_BYPASS_HOOK_TRUST_FLAG` + min-version gates (policy hooks ≥0.129.0, bypass-trust ≥0.131.0) — defined, not yet applied to omnigent-launched argv | `omnigent/inner/codex_native_app_server.py:89-94,1877` |
| Fork detection (session-level: `/fork`, `/branch`, `forkedFrom` markers) | `claude_native_forwarder.py:233,2311,2457`; `claude_native_bridge.py:1716` |
| Existing hook-script precedents (no shared dir yet) | `omnigent/inner/cursor_policy_hook.py`, `omnigent/inner/hermes_policy_hook.py` |
| Telemetry: `omnigent` tracer, `span()` ctx manager, `record_llm_usage`/`record_error`; **no event-emission helper yet** | `omnigent/runtime/telemetry.py:598,632,789,816` |

**Gaps this plan closes:** (G1) native in-harness subagents are unrouted —
Claude SDK executor registers **no** hooks today, Claude-native's PreToolUse
hook doesn't cover Task spawns, Codex symlinks the user's `hooks.json`
untouched; (G2) router-contract knowledge is smeared across the client
(static `MODEL_LISTS`, post-correction) instead of the §2 seam, and
`route_selector.config` is never sent; (G3) no routing telemetry, and
`RoutingDecisionData` lacks harness/scope/decision identity; (G4) decisions
are not visible per-subagent — `ChildSessionInfo`
(`web/src/hooks/useChildSessions.ts:25-54`, fed by
`GET /v1/sessions/{id}/child_sessions`) carries **no model field at all**, and
there is no generic session-header warning banner to surface enforcement
state.

Enforcement primitives verified externally (2026-07-28 research reports):

- **Claude Code**: `PreToolUse` hook on the Agent/Task tool can deny **and**
  rewrite `tool_input` (incl. `model`) via `hookSpecificOutput.updatedInput` +
  `permissionDecision: "allow"`. Settings-level hooks recurse to nested
  subagents. Works in CLI and Agent SDK (SDK: in-process hook callbacks).
- **Codex** (live-verified on codex-cli 0.145.0): Claude-compatible hooks;
  `PreToolUse` on `spawn_agent` can deny and rewrite args **including
  injecting `model`** (not LLM-visible in the schema; harness accepts it).
  Caveats: flattened tool name is `collaborationspawn_agent` — match
  `.*spawn_agent` by regex; the spawn `message` field is **encrypted** in hook
  payloads (route on `task_name` + metadata, never prompt text); unmanaged
  hooks are **silently skipped** unless trusted; `SubagentStart` payload
  carries actual `agent_id` + `model` for audit.

---

## 4. Task packets (optimized for parallel Opus 5 subagents)

Rules of engagement for subagents:

- One packet = one agent = an **exclusive set of owned files**. Never edit
  another packet's files; integration points go through the frozen contracts
  in §5. Shared-file exceptions are called out explicitly per packet.
- Every packet lands its own unit tests alongside the code and must pass
  `uvx pre-commit run --files <owned files>` + its own test targets
  (`uv run pytest <paths>`; `cd web && npx vitest run <paths>` — the web
  suite is **vitest**, not jest) before committing to `routing-mvp`.
- Commit per packet (small, revertable), all on `routing-mvp`.

### Wave 1 — six packets, fully parallel

**P1 — Route-options seam + config + default-on AIGW router** *(req 1, G2)*
Owns: `omnigent/server/smart_routing.py`, `omnigent/cli.py` (routing-client
build region ~107-166 only), `tests/server/test_smart_routing.py`.
- Introduce `RouteOptionSource` per §2; move `_HARNESS_EXCLUDED_MODELS` /
  `_redirect_incompatible_pick` / `MODEL_LISTS` behind it (task_v1 scenario
  menus per §1, config-overridable via `routing.scenario_menus`, keyed by
  router version; default `routing.router_name` = `task_v1`).
- Map unservable picks to the nearest servable catalog id; keep the raw pick
  for the decision payload (UI shows what the router said).
- Populate `route_selector.config.model` from new `routing.selection_model`
  (proto field already exists — zero proto work); keep raw prompt + existing
  4000-char cap.
- **Owns ALL new `routing.*` config parsing** (including P2's
  `subagent_fail_mode` and cache TTL) into the frozen `RoutingSettings`
  dataclass (§5.4) hung on `RuntimeCaps` — other packets read the dataclass,
  never cli.py. This is what keeps cli.py single-owner.
- Default-on: when the server's provider is Databricks (`kind: databricks`)
  and no `routing:` block exists, synthesize the external client against that
  workspace's `/ai-gateway/routing/v1` with
  `model_prefixes=["databricks-", "system.ai."]` and profile auth. Explicit
  `routing:` config always wins; `routing.provider: none` opts out.
- Failure semantics unchanged for sessions/turns: `None` + `last_error`,
  caller proceeds unrouted with the reason (§2) — do NOT invent a runtime
  LLM-judge chain in this packet.

**P2 — Runner `route-subagent` endpoint** *(req 2 backbone)*
Owns: new `omnigent/runner/subagent_routing.py`, its wiring into the runner's
existing local HTTP surface, new `tests/server/test_subagent_routing.py`.
- **Follow the existing tool-relay pattern** (`claude_native_bridge.py:3291`):
  loopback HTTP on `127.0.0.1:0`, bearer token + URL advertised via a JSON
  file in the session bridge dir — do not invent a new auth scheme. Serves
  the §5.1 contract.
- Reaches the server's `RuntimeCaps.routing_client` the same way live
  catalogs flow today (the runner↔server hop behind
  `fetch_runner_models`, `smart_routing.py:122`) — P2 adds the inverse
  relay route next to the existing `/v1/sessions/{id}/models` handler.
- Policy lives server-side so hook scripts stay dumb: `fork=true` → `allow`
  unchanged (v1: don't route forks); router unreachable →
  `RoutingSettings.subagent_fail_mode` (`open`=allow unchanged [default],
  `closed`=deny) — pilot runs `closed`.
- Caches per (session, task-hash) with TTL to keep the blocking spawn path
  fast; cache retains per-session picks shaped for future `session_history`.
- Persists every decision as a transcript item via the §5.2 shape.
- Tests with `_FakeRoutingClient` + the `_caps` patch pattern
  (`tests/server/test_smart_routing.py:62,305-317`): rewrite / redirect /
  deny / fork-exempt / outage-open / outage-closed paths.

**P3 — Claude PreToolUse router hook (native + SDK)** *(req 2)*
Owns: `omnigent/claude_native_bridge.py` (the `build_hook_settings` region
only), new `omnigent/inner/hook_scripts/` dir (create it) + claude router
hook script, `omnigent/inner/claude_sdk_executor.py` (hook registration
only), matching tests.
- Native: extend `build_hook_settings` (`:1118-1364`) with a `PreToolUse`
  entry matching the Agent/Task tool — **model on the existing deny-capable
  PreToolUse policy-hook entry at `:1322-1325`** and the
  `cursor_policy_hook.py` / `hermes_policy_hook.py` script precedents.
  Script discovers the P2 endpoint via the bridge-dir advertisement file
  (same discovery as `tool_relay.json`; bridge dir comes in on
  `HARNESS_CLAUDE_NATIVE_BRIDGE_DIR` / `--bridge-dir` argv). Stdlib-only,
  fast — it blocks spawns.
- SDK: `claude_sdk_executor.py` registers **no hooks today** (verified) —
  add the same decision logic as an in-process `claude-agent-sdk`
  `PreToolUse` callback; no subprocess.
- Decision mapping per §5.1: `rewrite` → allow + `updatedInput` with routed
  `model`; `redirect` → deny with reason
  `"Router selected <harness>/<model>. Use sys_session_send with args.harness=…, args.model=… instead."`;
  `deny` → deny with router reason. Fork-typed spawns send `fork=true`.
- Until P7 wires the live endpoint, develop against a stub honoring §5.1.
- Tests: fixture hook payloads → exact hook JSON out; settings-generation
  snapshot (extend existing `build_hook_settings` tests); recursion note
  (settings hooks apply to nested Task spawns).

**P4 — Codex hooks.json generation + trust + canary** *(req 2)*
Owns: `omnigent/inner/codex_executor.py`, `omnigent/inner/codex_native_app_server.py`
(argv/trust region only), codex router hook script in
`omnigent/inner/hook_scripts/`, matching tests.
- Stop symlinking the user's `hooks.json` when routing is on
  (`codex_executor.py:113,760`); generate a merged file: user hooks +
  Omnigent `PreToolUse` matcher `.*spawn_agent` (regex — flattened name is
  `collaborationspawn_agent` on 0.145.x; re-check inside the script),
  generous timeout, + `SessionStart` canary (touch file in bridge dir) +
  `SubagentStart` audit writer (`agent_id`, `model` → bridge dir).
- Trust: **the flag and version gates already exist** —
  `_CODEX_BYPASS_HOOK_TRUST_FLAG` and the ≥0.129/≥0.131 constants
  (`codex_native_app_server.py:89-94,1877`); apply the flag to
  omnigent-launched codex argv when the generated hooks file is in play,
  behind the existing version check. Document the managed
  `requirements.toml` path for fleet later. Canary absent after launch →
  emit the §5.3 warning event instead of failing open silently.
- Codex constraints: spawn `message` is encrypted — pass through verbatim on
  rewrite, route on `task_name`/parent-model/metadata only; injected `model`
  must come from the harness's spawn-eligible set or the handler errors.
- Tests: hooks.json merge snapshot (user hooks preserved), canary detection,
  audit-file parsing, argv assembly incl. version-gated flag.

**P5 — Decision data model + child-sessions API + telemetry** *(reqs 3+4 backbone)*
Owns: `omnigent/entities/conversation.py` (`RoutingDecisionData` only), the
`GET /v1/sessions/{id}/child_sessions` handler (routed-model field addition),
routing events in the `omnigent/telemetry` OSS usage-telemetry package,
matching tests.
- Extend `RoutingDecisionData` per §5.2 — additive, defaulted (current fields
  are exactly `model/applied/rationale/agent`; legacy rows must deserialize).
- Add `routed_model` (+ `routing_decision_id`) to the child-sessions API
  payload so the sidebar can render per-subagent models — **this is the
  server half of G4; P6 must not need server edits.**
- Telemetry: product analytics go through the OSS usage-telemetry pipeline
  (`omnigent/telemetry`), **not** `omnigent/runtime/telemetry.py` (which is the
  OTel tracing module). Add `RoutingDecisionEvent` and
  `RoutingSettingChangedEvent` to `omnigent/telemetry/events.py`, a
  `routing_enabled` flag on `SessionCreatedEvent`, and the recorders in
  `omnigent/telemetry/routing.py`. No free-form strings ship: the judge's
  rationale, routed prompt and subagent task name stay in the transcript, and
  model ids are reduced to allowlisted family/tier tokens by
  `omnigent/telemetry/model_labels.py` because a servable id can be a
  user-named workspace endpoint.
- Tests: serialization round-trip incl. legacy rows; child-sessions payload;
  event recording + wire format asserted in `tests/test_telemetry_routing.py`
  (pattern: `tests/test_telemetry.py`).

**P6 — Web UI: per-subagent visibility + warning banner + toggle telemetry** *(reqs 3+4)*
Owns: `web/src/` only — `StatusBlocks.tsx`, `Sidebar.tsx`,
`subagentStatus.ts`, `useChildSessions.ts`, `NewChatDialog.tsx`, vitest tests.
- Extend `RoutingDecisionChip`/`RoutingDecisionCard`
  (`StatusBlocks.tsx:130,172`) with harness + scope badge and raw-pick vs
  applied model.
- `useChildSessions.ts`: add the §5.2-mirrored `routed_model` field to
  `ChildSessionInfo`; render it on sidebar child rows (ankit req #1).
  Develops against fixture payloads matching P5's API addition.
- **Build the session-header warning banner** (none exists — verified; the
  closest precedents are `ReconnectSessionDialog` states and the sidebar
  `AlertTriangleIcon` usage) and render §5.3
  `subagent_routing_unenforced` on it.
- Fire switch-off/fork telemetry triggers through the existing event
  plumbing when the user leaves IR mid-session or forks a routed session.
- NewChatDialog: leave the dead `smartRoutingEligible` sentinel untouched
  this wave (design decision with Ajay/Tomu pending — §8).
- Tests: **vitest** (`cd web && npx vitest run src/...`), not jest.

### Wave 2 — integration (start once the relevant Wave-1 packets merge)

**P7 — Hook↔endpoint integration + override precedence** *(needs P2+P3+P4)*
The only packet allowed to touch multiple packets' files.
- Wire the real P2 endpoint advertisement into the generated Claude settings
  and Codex hooks.json (bridge-dir file), replacing the P3/P4 stubs.
- Override precedence: when routing is on, an LLM-supplied `args.model` on
  `sys_session_send` must NOT override the router. Note `args.model` is
  **create-time-only** (`spawn.py:1778`) — the precedence gate goes where
  `model_override` enters `create_body`, and the attempted override lands in
  the decision item (`attempted_override`, §5.2). Add the test.
- `SubagentStart`-vs-decision reconciliation in the codex forwarder;
  mismatch → §5.3 warning event.
- Integration tests: fake router + `ControllableMockClient`
  (`tests/server/conftest.py:177`), extending the
  `tests/server/integration/test_sessions_child_sessions.py` pattern —
  spawn rewritten in-harness, blocked cross-harness with redirect text,
  transcript item present, child-sessions API carries the routed model.

**P8 — Live E2E + probes + version pin** *(needs P7)* — see §6 for the full
test matrix this packet executes.
- Add a codex version pin: `harness_install_spec.py` has `min_version` /
  `max_version_exclusive` fields but **no codex pin today** (verified) — set
  one covering the hook-verified range (≥0.145.0, < next-untested-major).

### Parallelism summary

```
Wave 1 (parallel):  P1   P2   P3   P4   P5   P6
                      \   |  /  \  |   /   |
Wave 2:                P7 (P2+P3+P4[+P1])  P6 finishes against P5 fixtures
Wave 3:                P8 (all)
```

File-ownership conflicts eliminated by construction: cli.py + smart_routing.py
are P1-only (P2 reads `RoutingSettings`, not config); entities + server API
additions are P5-only (P6 consumes fixtures); the two executors split cleanly
(P3 = claude files, P4 = codex files); `omnigent/inner/hook_scripts/` is new
but P3/P4 add disjoint files inside it. P7 is the sole integrator.

---

## 5. Frozen interface contracts (agents code against these, not each other)

### 5.1 `route-subagent` endpoint (P2 serves; P3/P4 consume)

Advertised to hook scripts via a bridge-dir JSON file
(`subagent_router.json`: `{url, token}`), same pattern as `tool_relay.json`.

`POST {url}/v1/sessions/{session_id}/route-subagent` (Bearer token)

```json
// request
{
  "harness": "claude-sdk" | "codex" | "claude-native" | "codex-native",
  "task_name": "string",            // subagent_type / task_name
  "prompt": "string | null",        // null on codex (encrypted upstream)
  "fork": false,
  "parent_model": "string | null"
}
// response
{
  "action": "allow" | "rewrite" | "redirect" | "deny",
  "model": "string | null",         // servable id, set for rewrite/redirect
  "harness": "string | null",       // set for redirect
  "raw_model": "string | null",     // router-vocabulary pick, pre-resolution
  "rationale": "string",
  "decision_id": "uuid"
}
```

### 5.2 `RoutingDecisionData` additions (P5 defines; P2/P6/P7 consume)

Today: `model: str`, `applied: bool`, `rationale: str`, `agent: str | None`.
Additive fields: `harness: str | None`,
`scope: Literal["session","turn","child_session","native_subagent"]`,
`decision_id: str | None`, `raw_model: str | None`,
`attempted_override: str | None`. All defaulted for legacy rows.
Child-sessions API mirrors `routed_model: str | null` +
`routing_decision_id: str | null` per child row.

### 5.3 Canary warning event (P4 emits; P6 renders)

Session-scoped warning `subagent_routing_unenforced` with `{harness, reason}`,
delivered on the existing session-status channel.

### 5.4 `RoutingSettings` (P1 defines & parses; P2 reads)

Frozen dataclass on `RuntimeCaps`:

```python
@dataclass(frozen=True)
class RoutingSettings:
    router_name: str = "task_v1"
    selection_model: str | None = None          # -> route_selector.config.model
    scenario_menus: Mapping[str, Mapping[str, tuple[str, ...]]] = TASK_V1_MENUS
    subagent_fail_mode: Literal["open", "closed"] = "open"
    subagent_cache_ttl_s: float = 300.0
```

---

## 6. Testing plan

Layered; each layer names its runner and when it gates.

**L1 — per-packet unit (gates every commit).**
`uv run pytest tests/server/test_smart_routing.py tests/server/test_subagent_routing.py <packet tests>`
and `cd web && npx vitest run src/...`. Baseline to protect: the existing 47
tests in `test_smart_routing.py` (they lock prefix round-tripping, worker-name
mapping, redirect correction, last_error surfacing) must stay green through
the P1 refactor — they are the regression net for the seam extraction.

**L2 — router contract fixtures (gates P1).** Freeze §1 as recorded
request/response fixtures and assert the client against them: (a) codex-only
harness set → request contains exactly the Codex arm menu (+catalog extras);
(b) claude-only → Claude arms; (c) mixed → all five; (d) 400 "requires its
full menu" → `None` + `last_error`, session proceeds unrouted; (e) pick of an
endpoint-less arm (`gpt-5-6-sol`) → resolved to a servable id with `raw_model`
preserved; (f) echoed nonsense harness ignored (harness derived from arm
family); (g) `route_selector.config.model` present iff `selection_model` set.

**L3 — live contract probe (manual/CI-cron, not commit-gating).**
`scripts/probe_routing_api.sh` — the recorded curl battery from 2026-07-28/29
(scenario inference, full-menu 400s, extras tolerated, tag passthrough)
against eng-ml-inference staging via `databricks auth token`. Run before
demos and whenever AIGW deploys; alerts us if a task_v2 lands or menus move.

**L4 — hook-layer unit (gates P3/P4).** Hook scripts are pure functions
around the §5.1 call: fixture stdin payloads → exact hook JSON out for
allow/rewrite/redirect/deny × fork × endpoint-down (×fail_mode). Codex
additionally: merged-hooks.json snapshot preserving user hooks; canary
present/absent; audit-record parsing; version-gated argv flag.

**L5 — server integration with fake router (gates P7).** Boot the test
server (`ControllableMockClient` + `_caps` patch), drive a session:
1. Omnigent child spawn (`sys_session_send`) → router decision wins over
   `args.model`, attempted override recorded.
2. Native-subagent decision via the P2 endpoint → transcript
   `RoutingDecisionData(scope="native_subagent")`, child-sessions API row
   carries `routed_model`.
3. `subagent_fail_mode=closed` + dead router → deny; `=open` → allow
   unchanged; both leave a decision item.
4. Decision cache: two identical spawns, one router call.

**L6 — live-harness E2E (gates P8; needs real claude/codex CLIs).**
Conventions from `tests/e2e/test_polly_subagent_model_e2e.py`:
- Claude native: session with router hook → Task spawn's model rewritten
  (assert via hooks.jsonl SubagentStart mirror).
- Claude SDK: same via in-process callback.
- Codex: `spawn_agent` rewritten; `SubagentStart` payload model ==
  decision_id's model (audit reconciliation); canary fires; with hooks
  untrusted and bypass flag stripped → canary absent → warning event.
- Cross-harness: deny+redirect reason emitted; model follows with
  `sys_session_send` at least once (track follow rate, don't hard-assert).
- Standalone `scripts/probe_codex_hooks.py` (deny + rewrite smoke) —
  rerun on every codex version bump; paired with the P8 version pin.

**L7 — manual CUJ pass on the dev stack (release gate, ~30 min).**
Stack: `./run-server.sh` / `./run-host.sh` / `./run-frontend.sh` in
`~/omnigent-routing-mvp` (ports 6868/5273, isolated config, staging AIGW,
`task_v1`). Checklist mirrors the brainstorm CUJs:
1. **Codex CUJ**: codex harness + IR on → server log shows Codex-arm-menu
   request; picked model applied; subagent spawn shows decision in
   transcript + sidebar.
2. **Claude Code CUJ**: same with claude; verify `/model` injection on a
   routed turn.
3. **Auto CUJ**: Polly + AUTO gear → harness+model pick lands; Omnigent
   child sessions routed via parent catalog; cross-harness redirect visible.
4. **Visibility**: every decision has a chip/card with rationale; per-subagent
   model in sidebar; kill the router mid-session → fail-mode behavior +
   warning banner.
5. **Telemetry**: `omnigent.routing.*` events visible in OTel export
   (`OTEL_EXPORTER_OTLP_ENDPOINT` set); switch IR off mid-session and fork a
   routed session → both events present.
6. **Isolation regression**: user-level `~/.omnigent` untouched (config home
   + data dir remain worktree-local).

**L8 — full-suite regression (before PR split).**
`uv run pytest tests/server tests/runtime tests/inner` +
`cd web && npx vitest run` + `uvx pre-commit run --all-files`.

---

## 7. MVP definition of done (Jul 31)

- AIGW `routes:select` drives session/turn/child routing by default on
  Databricks-backed deployments (P1) using `task_v1`, surviving the confirmed
  contract quirks: fixed scenario menus, passthrough harness tags, unservable
  picks. L2 fixtures green; L3 probe clean against staging.
- A Claude Task spawn and a Codex `spawn_agent` cannot proceed on a
  non-router-approved model; failure mode is "didn't spawn", never "wrong
  model" (P2–P4, P7). L5/L6 green.
- Every decision renders in the transcript; per-subagent routed model in the
  sidebar; warning banner when enforcement is off (P5, P6).
- `omnigent.routing.*` OTel events for decision / enabled / switched-off /
  fork (P5, P6).
- L7 manual CUJ pass recorded (notes or screen capture) on the live dev stack.

## 8. Outside this branch / open items

- **SAFE flag** for isaac default-on cohort — universe repo, after this branch
  is testable.
- **NewChatDialog reconciliation** (Auto harness vs per-harness IR toggle,
  naming "Intelligent Routing") — blocked on Ajay/Tomu design call.
- **v3 gateway model-listing API** — explicitly not-MVP per the checklist.
- **Feed to Mason**: (a) harness-constrained returns (client correction is a
  stopgap — §1); (b) spawn-eligible model subsets per harness (Codex spawns
  support fewer models than sessions); (c) menu ids should not be required
  when absent from the caller's workspace.
- **Feed to Ivan**: encrypted Codex spawn prompts cap task-aware quality for
  in-harness codex subagent routing (signal lives in `task_name` only).
- **Routing-availability liveness probe** — §10 decision 9 gates on
  config-level availability only; a gateway that is configured but down still
  offers Smart Routing. Follow-up, not MVP.
- **Move the `routes:select` call host-side** so routing auth/workspace always
  matches the host's inference; availability is already host-derived.

## 9. Risks

1. **Codex hook fragility** (tool-name flattening, trust gate, v1/v2
   selection): regex matcher + canary + new version pin + L6 standalone
   probe; budget breakage on bumps. Groundwork (bypass flag + version gates)
   already exists in `codex_native_app_server.py`.
2. **Router latency blocks spawns**: task_v1 always makes an LLM extraction
   self-call (§1.1), so p99 sits on the PreToolUse path; mitigated by P2
   caching + fail-mode knob; measure in L6.
3. **Redirect compliance is soft**: deny+redirect relies on the model calling
   `sys_session_send`; worst case non-spawn, never wrong-model. Track
   redirect-follow rate via decision items during the pilot.
4. **Scenario-menu drift**: routers are frozen per version (§1.1), so menus
   only change when we bump `routing.router_name` — `scenario_menus` keyed by
   router version keeps them moving together; L3 probe catches server-side
   surprises; P1 degrades to unrouted-with-warning on mismatch.
5. **Fork/cache-miss economics**: v1 doesn't route forks; a real cost model
   needs inherited-context size that only Omnigent can supply out-of-band.
6. **Extraction-model access**: task_v1's self-call needs the caller to have
   `system.ai.gpt-5-4-mini` (or `routing.selection_model` pinned to one they
   do have); a workspace without it breaks routing invisibly — L3 probe +
   `last_error` surfacing cover it.

## 10. Product UX decisions (Bryan, 2026-07-29)

Resolves the **NewChatDialog reconciliation** open item in §8 (previously
blocked on the Ajay/Tomu design call) and supersedes the §4 P6 instruction to
"leave the dead `smartRoutingEligible` sentinel untouched". Decisions 1–3 are
shipped on `routing-mvp`; decision 5 is in flight.

1. **Intelligent Routing is a Model choice in the per-harness config modal.**
   *Configure Claude Code* / *Configure Codex* (new-chat landing) offer
   "Intelligent Routing" in the Model ("Underlying LLM") dropdown, gated on the
   server's `smart_routing_enabled` capability and only for `claude-code` and
   `codex`. Picking it disables the Effort row to an em-dash ("—") — the router
   picks effort per task, so showing a live value would lie; the Permissions row
   is unchanged. The session is created with `cost_control_mode_override: "on"`
   and **no** model/effort pin, i.e. exactly the session-start routing path
   already wired at `smart_routing.py:823` ← `orchestration.py:3748` (§3).
   Rationale: routing is a property of *which model runs*, so it belongs in the
   Model dropdown rather than as a fourth control users must discover. **SHIPPED**
   (web `374d267c`, `320b6b59`).

2. **Fully-auto mode is named "Smart Routing"** (superseding the earlier
   "Auto" decision). The label iterated "Intelligent Routing" → "Auto
   Harness" → "Auto" → **"Smart Routing"** (Bryan, 2026-07-29: one name
   everywhere — the harness, both model selectors, the subagent selector;
   `e5c8a160`). The dropdown row sits in its own unlabeled group above the
   Harnesses group with no helper text (Ajay's review, `76749e03`).
   Rationale unchanged: the chip is a glance-level affordance; explanation
   belongs in the hover description, not the label. **SHIPPED**.

3. **Configure Auto shows only Permissions, locked to a disabled "Default".**
   No Model or Effort rows (the router owns both), and the create payload
   carries **no permission override at all** — the picked harness inherits the
   machine's own Claude Code / Codex default config, byte-identical to launching
   `claude-code` natively on its default permission mode. A cross-harness common
   permission mapping was researched today (Claude permission modes vs Codex
   `approval_policy` × `sandbox` × permission profiles; proposal: Read Only /
   Default / Auto / Full Access) and is **deliberately deferred** — the four-way
   mapping has enough asymmetry that shipping it wrong would silently loosen
   sandboxing. Showing the row disabled keeps the slot visible for when the
   mapping lands and unlocks the remaining options. **SHIPPED** (`320b6b59`);
   the mapping write-up lives in the session scratchpad and moves into
   `designs/` when adopted.

4. **Main-agent routing is SESSION-START ONLY** (affirmed by Bryan 2026-07-30).
   The router runs **once**, on the session's first message; the model it picks
   **persists for the life of the session**; later turns **do not re-route**,
   however different they look from the first. This holds for all three CUJs —
   Claude Code, Codex and the Smart Routing harness (decision 7 restates it for
   the harness pick, which is additionally physical). A brief per-turn
   re-routing experiment was implemented and live-verified on 2026-07-30, then
   reverted the same day on Bryan's product decision (§12); per-turn routing
   remains gated on the router's unused `session_history` field (§1.1) and is
   out of MVP scope. Because routing pins the model at session start, an
   in-session "Model = Intelligent Routing" toggle would imply a switch that
   cannot take effect. This is why `costRoutingEligible` deliberately stays off
   for native sessions — the dead sentinel in §3
   (`HarnessConfigControls.tsx:17`, `NewChatDialog.tsx:2165`) is now
   intentional, not an oversight. The in-session control is instead a
   per-session **Subagent routing** setting (decision 5), which *is* meaningful
   mid-flight because it only affects future spawns.

5. **New per-session setting `subagent_routing_override` (`"on"` / `"off"` /
   `null`).**  The in-session gear for Claude Code, Codex (native + SDK) and
   Auto sessions gains a "Subagent routing: Intelligent Routing / Default" row,
   toggleable at any time and effective on the next spawn. `null` (default)
   **inherits the session-start choice**: an IR main agent routes its subagents,
   a manually pinned model does not. Implementation consequences: the §5.1 relay
   gate must re-check the setting **per call** rather than at launch — this also
   fixes a launch-time lock-in bug where a routed session enforced subagent
   routing forever — and the §4 P3/P4 hooks must be installed whenever the
   server has routing capability, since a session that starts unrouted can be
   toggled on later. **SHIPPED** (per-call gate + web row with explicit
   Inherit option; evidence in CUJ_STATUS §2.5, last re-verified 2026-07-31).

6. **Closes the Jul 28 meeting-note requirement** "toggle for subagent routing
   as well as main agent routing", which the CUJ audit flagged as unimplemented:
   decision 1 covers main-agent routing at session start, decision 5 covers
   subagent routing at any time.

7. **Top-level Smart Routing sessions are session-pinned; no per-turn
   re-routing (Bryan, 2026-07-29; re-affirmed 2026-07-30).**  The agentless
   "Smart Routing" harness routes once, at session start, over the five-arm
   `both` menu, and the pick (harness *and* model) holds for the life of the
   session. Later turns do not re-enter `routes:select`, even when they look
   nothing like the first one.
   Rationale: the harness pick is physical — a session *is* a live `claude` or
   `codex` process with its own bridge, config and pane — so "re-route turn 2"
   means killing and relaunching a process mid-conversation. Consistent with
   decision 4 (main-agent routing is a session-start concept); per-turn
   routing waits on `session_history` (§1.1) and is out of MVP scope.

8. **Routed `/model` writing the user's claude default is accepted for now.**
   Applying a routed model to a native Claude Code pane types `/model <alias>`
   into the TUI, and Claude Code persists that choice as the machine's default
   model — so a routed session leaves the user's next *manual* `claude` launch
   on the routed arm. This matches the pre-existing behavior of the harness
   config modal's Model picker (which drives the same `/model` path), so
   routing introduces no new surprise. Not worth a save/restore dance for MVP;
   revisit if a non-persisting model API lands upstream.

9. **Smart Routing is only offered where the apply layer can work (Bryan,
   2026-07-31).**  Availability is now *routing available* (config-level: the
   server has a routing client — the existing `smart_routing_enabled` surface)
   **AND** the selected host's inference for that harness family being
   AI-Gateway-backed. Each of the three surfaces gates independently:
   *Configure Claude Code* → Model needs the host's claude-native launch to
   resolve the gateway env (`ANTHROPIC_BASE_URL` + api-key helper — the
   resolution the runner logs as `configured=True`); *Configure Codex* → Model
   needs the host's codex provider `base_url` to be on the workspace AI Gateway
   (the `…/ai-gateway/codex/v1` family); the top-level **Smart Routing** harness
   row needs **both**, since it routes over the five-arm `both` menu.
   The signal is a per-host, per-harness fact computed host-side by reusing the
   launch config resolutions as a cheap check (config resolution only — no
   launching, no network), reported as a `gateway_inference` map alongside
   `configured_harnesses` in the host readiness surface and echoed by
   `GET /v1/hosts`. **Absent means unknown and does not gate** (hosts on older
   builds keep every option), to be tightened once hosts have rolled forward.
   Web classification stays in the single `smartRoutingAvailability.ts` point as
   a new `not-gateway-backed` cause, ordered after `harnesses-unready`.
   Rationale: a routed session on a non-gateway host resolves a model the pane
   cannot reach, so the pick is worse than no pick. Deliberately **no liveness
   probing** — config-level availability only (see §8).

---

## 11. Canonical CUJ test matrix (Bryan, 2026-07-29)

The referencable pass for "is Smart Routing actually working". Same four
prompts every time, so results are comparable across runs and across the
manual UI pass and the headless driver. Expected routes are **derived from the
frozen `task_v1` recipe** (§1), not from what we hope happens:

- **`cc` scenario** (Claude arms only): rule-0 → `claude-sonnet-5` (<300 chars,
  no code refs, `difficulty == easy`, cheapest arm, never escalates); else if
  `not_crosscutting AND not_mixed_change AND low_ambiguity` all hold → escalate
  `claude-opus-4-8`; else default `claude-sonnet-5`.
- **`codex` scenario** (Codex arms only): rule-0 → `gpt-5-6-luna`; else if
  `not_crosscutting AND prompt_short` both hold → delegate `glm-5-2`; else
  default `gpt-5-6-sol`.
- **`both` scenario** (all five arms): rule-0 → `gpt-5-6-luna`; else the
  three-way conjunction all-holds → escalate `claude-opus-4-8`; else default
  `gpt-5-6-sol`.

**The bar is `raw_model == applied_model`.** A decision chip showing a
substitution arrow (router picked X, we ran Y) is a **failure**, not a
tolerated degrade — the two worst bugs on this branch were caught exactly that
way. One exception is tracked in row C1 below.

### 11.1 The four canonical prompts

**P-OPUS** — clear, contained, code-referencing feature work (620 chars). Fails
rule-0 (too long, backticked symbol) and satisfies the whole conjunction, so
the `cc`/`both` recipes escalate to opus:

```
Add a --dry-run flag to our `deploy` CLI command. When passed, the command should resolve the full deployment plan — the ordered list of services to update, the target version for each, and any config changes — and print it as a human-readable table, then exit 0 without calling the orchestration API or mutating any state. Without the flag, behavior is unchanged. Reuse the existing plan-resolution logic from the real run (do not duplicate it) so the dry-run output always matches what a real deploy would do, and document the flag in the command's help text. Add tests asserting no API calls are made in dry-run mode.
```

**P-GLM** — narrow, well-specified bug fix (604 chars), inside the
`prompt_short` bucket and not crosscutting, so the `codex` recipe delegates to
glm:

```
The `parse_duration` helper in our config module returns None for values like "1h30m" because its regex only matches a single unit group, so any compound duration silently becomes None and the caller falls back to the default timeout. Fix the parser to accept compound durations combining days, hours, minutes, and seconds (e.g. "1h30m", "2d4h", "45s") and return the total number of seconds as an int. Preserve the current behavior for single-unit values and for the empty string, and raise a ValueError on genuinely malformed input instead of returning None. Add unit tests covering the compound cases.
```

Length matters here: keep P-GLM under ~1.2k chars. A longer variant fails
`prompt_short` and the codex recipe falls through to `gpt-5-6-sol` — a green
run on a bloated prompt proves nothing about the glm arm.

**P-SOL** — the AIGW Intelligent Routing Brainstorm doc pasted whole (long,
cross-cutting, many surfaces and open questions). Not embedded here; paste the
doc verbatim from the meeting notes. `not_crosscutting` fails, so `codex`/`both`
land on the `gpt-5-6-sol` default and `cc` falls back to `claude-sonnet-5`.

**P-TRIVIAL** — literally:

```
hi
```

Under 300 chars, no code refs, easy → rule-0 fires. That means
**`gpt-5-6-luna`** on `codex`/`both` and **`claude-sonnet-5`** on `cc` (rule-0
picks the scenario's cheapest arm, and the `cc` menu has no luna).

> **`gpt-5-4-mini` is not an arm.** It is task_v1's *extraction* model (§1.1),
> called on every routing request to score change scope / ambiguity /
> difficulty. "hi → 5-4-mini" is a misread of the recipe; if a decision ever
> *applies* `gpt-5-4-mini`, that is a bug in our option list, not the router.

**Probing luna specifically.** luna is rule-0's arm on every Codex-bearing
scenario, so any short, code-free, easy prompt lands it — useful when you want
a cheap smoke test rather than the full battery. Known-good examples: `hi`,
`what time is it?`, `summarize what this repo does in two sentences`.

### 11.2 Matrix

Every row asserts four things: the decision exists, the decision is the
expected one, the **applied** model equals the raw pick, and the harness
process is really on that model.

**Verification handles** (same for all rows):
- **Chip** — routing chip rendered under the user message, no substitution arrow.
- **Card** — decision card / rationale expands and names the predicates.
- **Claude process truth** — `tmux capture-pane` on the session's pane: the
  injected `/model <alias>` line and the resulting model banner.
- **Codex process truth** — the TUI status-bar model, the session's
  `config.toml` under the bridge dir's codex-home, and the newest rollout
  `.jsonl` in that codex-home's `sessions/`.

#### A. Top-level Smart Routing harness (`both` scenario, all five arms)

| # | Prompt | Expected decision | Expected APPLIED model | Verify |
|---|---|---|---|---|
| A1 | P-OPUS | conjunction all-holds → escalate | `claude-opus-4-8` (harness: claude-code) | chip + card; claude pane banner + `/model` echo |
| A2 | P-GLM | conjunction all-holds → escalate | `claude-opus-4-8` (harness: claude-code) | as A1 |
| A3 | P-SOL | crosscutting → default | `gpt-5-6-sol` (harness: codex) | chip + card; codex status bar + `config.toml` + rollout jsonl |
| A4 | P-TRIVIAL | rule-0 → cheapest arm | `gpt-5-6-luna` (harness: codex) | as A3 |

Session-scope decision only (§10 decision 7) — turn 2 must **not** produce a
second session-scope decision. Subagents here **may** be either family;
cross-harness spawns are legal in scenario A and only here.

#### B. Claude Code + Model = Smart Routing (`cc` scenario, Claude arms only)

| # | Prompt | Expected decision | Expected APPLIED model | Verify |
|---|---|---|---|---|
| B1 | P-OPUS | conjunction all-holds → escalate | `claude-opus-4-8` | chip + card; pane banner Opus after `/model opus` |
| B2 | P-SOL (conjunction-failing) | crosscutting → default | `claude-sonnet-5` | chip + card; pane banner Sonnet |
| B3 | P-TRIVIAL | rule-0 → cheapest arm | `claude-sonnet-5` | as B2 |
| B4 | Task spawns (Explore, general-purpose, …) | one decision per spawn, routed on the **Task prompt** | a Claude arm — never a Codex arm | chip per spawn + sub-agents panel model |
| B5 | Gear → Subagent routing = **Default** mid-session | next spawn produces **no** decision chip | spawn proceeds on harness default | flip, spawn immediately, confirm chip absent |
| B6 | Gear → Subagent routing = **Intelligent Routing** again | chips resume on the very next spawn | routed Claude arm | as B4 |

B5 is the strict form of the mid-session toggle: the gate is re-checked **per
call**, so suppression must be visible on the *next* spawn, not "eventually".

#### C. Codex + Model = Smart Routing (`codex` scenario, Codex arms only)

| # | Prompt | Expected decision | Expected APPLIED model | Verify |
|---|---|---|---|---|
| C1 | P-GLM | `not_crosscutting AND prompt_short` → delegate | `glm-5-2` | codex status bar + `config.toml` + rollout jsonl — see the flag below |
| C2 | P-SOL | crosscutting → default | `gpt-5-6-sol` | as C1 |
| C3 | P-TRIVIAL | rule-0 → cheapest arm | `gpt-5-6-luna` | as C1 |
| C4 | Named `spawn_agent` calls | one decision per spawn, routed on the task/agent name | a Codex arm — never a Claude arm | chip per spawn + SubagentStart audit record |
| C5 | Unnamed spawns | routed on the `"Codex subagent task"` placeholder (short, code-free) | `gpt-5-6-luna` | as C4 |
| C6 | Mid-session Subagent routing → Default, then back on | chips stop on the next spawn, resume when re-enabled | harness default, then routed Codex arm | as B5/B6 |

> **C1 flag (tracked, the only tolerated fallback).** `glm-5-2` must appear in
> the codex model list for the session to *run* it — that list is
> client-side in codex/ucode and today has no glm entry (§CUJ status), and it
> must also pass omnigent's family whitelist. Until the list lands, C1 shows a
> substitution arrow (`glm-5-2` → a servable Codex arm). This is the **only**
> row where an arrow is accepted, it is an external isaac/ucode dependency, and
> it stays on the tracked list until fixed. Every other arrow is a bug.

#### D. Global assertions (apply to every row above)

1. **No fallback arrows.** `raw_model == applied_model` on every decision,
   C1 excepted.
2. **Every decision is visible** as both a chip (paired under the triggering
   user message) and an expandable decision card with the router's rationale.
3. **Cross-harness spawns appear only under A.** A `cc` session must never
   spawn a Codex arm and a `codex` session must never spawn a Claude arm — the
   same-harness constraint covers native spawns *and* omnigent child sessions.
4. **Process truth beats UI.** A row is only green when the harness process
   confirms the model; a chip alone is 🟡.

### 11.3 Headless driver recipe

For the fast, repeatable pass (no clicking). Same server as the manual stack.

1. **Create the session** — `POST /v1/sessions` with `agent_id`, `host_id`,
   `workspace`, and `cost_control_mode_override: "on"`, and **no** model or
   effort pin. That is exactly what the Model = Smart Routing path sends
   (§10 decision 1); a pin silently disables routing.
2. **Send a turn** — `POST` the session's events endpoint with

   ```json
   {"type": "message", "data": {"role": "user", "content": [{"type": "input_text", "text": "<canonical prompt>"}]}}
   ```

   Paste the prompt raw — `task.prompt` is the entire routing signal (§1.1),
   so any wrapper or summary changes the answer.
3. **Read the decisions** — query the dev DB
   (`$OMNIGENT_DATA_DIR/chat.db`):

   ```sql
   SELECT * FROM conversation_items WHERE data LIKE '%rationale%' ORDER BY rowid;
   ```

   Each row carries the scope, the raw pick, the applied model and the router
   rationale — enough to score raw-vs-applied without the UI.
4. **Codex ground truth** — in the session's bridge dir, read the codex-home
   `config.toml` (`model = …`) and the newest rollout `.jsonl` under
   `sessions/`; the rollout is what the process actually ran.
5. **Claude ground truth** — take the `tmux_socket` from the runner log for the
   session and `tmux -S <socket> capture-pane -p` the pane: assert the injected
   `/model` line and the banner that follows it.

### 11.4 Stack

```sh
./run-server.sh      # :6868, isolated OMNIGENT_CONFIG_HOME/OMNIGENT_DATA_DIR
./run-host.sh        # runner against localhost:6868
./run-frontend.sh    # :5273
```

Staging AIGW, `router_name: task_v1` (§1.1 dev-loop config). The manual pass
uses the same three processes as the headless driver, so a headless red row can
be re-checked by hand without restarting anything.

---

## 12. Implementation deltas (what reality forced us to change)

Written after the branch went end-to-end (§11 matrix at 14/14 exact). Each
delta: what the plan assumed → what we found → what shipped. Commit shas in
parentheses. Cross-references name the plan section the delta contradicts or
extends.

### Claude Code apply layer

1. **`/model` injection was already wired, but the model never arrived.**
   §3 listed routing with "native `/model` injection" as existing
   capability, so the plan treated applying a routed model to a Claude pane as
   solved plumbing. In reality the runner's `_run_turn_bg` rebuilt the harness
   request field by field and never copied `model_override` off the incoming
   message, so the executor's injection branch never ran — routed sessions
   silently kept their launch model while `/effort` (a separate session change)
   worked. The field is now forwarded explicitly with INFO logs at every hop
   (server forward, runner intake, turn dispatch, executor type/skip-with-reason)
   so the chain cannot go silent again (`82cac6fa`).

2. **Claude's model vocabulary is closed, so servable catalog ids are not a
   spelling the harness accepts.** §2's `resolve_selection` assumed one
   resolution step — router vocabulary → servable catalog id — and that the
   servable id is what you hand the harness. But Claude Code's Agent tool
   `model` param is a closed alias enum and `/model` accepts only aliases, a
   byte-exact `ANTHROPIC_CUSTOM_MODEL_OPTION`, or an id a live endpoint probe
   admits; injecting a raw catalog id no-oped the main-session switch and
   hard-failed subagent spawns. A shared `claude_model_vocabulary` helper now
   inverts the `ANTHROPIC_DEFAULT_*_MODEL` pins (persisted into `bridge.json` so
   runner-side code can read the terminal's env), Smart Routing creates pin the
   routed id into the free custom picker slot, alias translation requires an
   **exact** pin match, routed-turn candidates are the pane's own vocabulary, and
   when no accepted spelling exists the decision records `applied=false` with
   the reason rather than claiming a model the process never ran
   (`539b00ae`, `af42b36c`). This is the delta behind §11's "no fallback arrows"
   bar being achievable at all: honest `applied=false` beats a silent lie.

3. **The workspace moved ahead of the frozen router arms.** §1.1 noted that
   frozen routers pin the *menu*; nobody noticed that the *workspace* keeps
   shipping newer generations. eng-ml-inference began serving `claude-opus-5`
   while task_v1's arms are `claude-opus-4-8` / `claude-sonnet-5`, and our
   newest-per-family alias pins drifted with it — so `/model opus` landed on
   opus-5 and the chip claimed the routed arm. Discovery now returns the full
   servable Claude catalog instead of newest-per-family, and alias translation
   requires an exact pin match (`af42b36c`); then, because claude-native launches
   its terminal *before* the first turn decision exists, launch-time alias pins
   were retargeted at the frozen arms' servable spellings (`opus` →
   `databricks-claude-opus-4-8`, arm list read from `TASK_V1` rather than
   duplicated) so turn-1 injection can reach the routed arm at all (`972dea9d`).

### Codex apply layer

4. **`--dangerously-bypass-hook-trust` is a no-op for app-server-dispatched
   hooks.** §3 recorded the bypass flag as existing groundwork and §9 risk 1
   assumed the trust gate was handled by it. A live probe matrix showed the
   generated routing hooks stayed untrusted and were silently skipped while the
   policy hooks worked — only the policy module's hashes were ever persisted.
   Both app-server launch paths now run a persisted trust handshake
   (`hooks/list` → `config/batchWrite` of the trusted hash → re-verify) for the
   router hook module, best-effort so a routing-trust failure can never disable
   the policy gate (`e32c4925`). The bypass-flag path was later deleted as dead
   surface (`d181cbd5`).

5. **Codex runs hook commands with the session workspace as cwd.** The plan's
   §5.1 hook scripts were "pure functions around the endpoint call" with no
   thought given to how they get imported. `python -m` puts cwd first on
   `sys.path`, so a workspace containing an `omnigent/` directory — the omnigent
   repo itself being the single most likely workspace — shadowed the installed
   package and every generated hook died on import, silently: routing gate,
   canary, spawn audit, and the policy hook alike. Hooks now run `python -I`
   (matching the bridge MCP command's posture), with a subprocess regression test
   that runs the real canary from a workspace containing a decoy package
   (`518376ba`).

6. **The enforcement canary was a circular detector, and it is what caught
   both codex bugs.** §5.3 specified the warning event; the watcher as built
   gated on the relay ledger that the broken hooks would have populated, so a
   total hook failure looked like silence. It now arms on the router
   advertisement and anchors on the first turn (codex fires `sessionStart` at
   first turn, not thread start) and posts `subagent_routing_unenforced` within
   a tick when the canary is absent (`e32c4925`); the message was widened to
   "untrusted, or the hook command failed" once cwd shadowing proved the second
   mode existed (`518376ba`).

7. **The routed codex model survived exactly one turn.** Neither §2 nor §3
   anticipated that applying a model to codex has three writers. `thread/settings/update`
   switched the thread but `config.toml` kept the launch default, the forwarder's
   `turn/started` mirror posted that default back as an `external_model_change`,
   and the next turn re-pinned it. Shipped: first-turn thread push, a
   `config.toml` mirror on a successful switch (the same key the TUI's `/model`
   writes, so the cost gate and the mirror agree), a `session.model` SSE so the
   web dropdown tracks live state instead of waiting for a reload (`0fcc313f`),
   and forwarder precedence that treats the last pushed thread model as the
   running thread's truth — a config value that changed since the previous read
   still wins, an unchanged one defers to the push (`51801530`). Surface audit in
   `designs/LIVE_MODEL_STATE.md`.

8. **A no-signal codex spawn has no routable task at all.** §1.1 and §8
   recorded that codex spawn `message` is encrypted and that routing must live on
   `task_name` + metadata; the plan assumed a name is always there. Live spawns
   frequently carry neither task nor agent name, and feeding the router an empty
   task produced a 400 that surfaced on the chip as a router outage. First we
   took ucode PR 251 parity — route unnamed spawns on the fixed placeholder task
   "Codex subagent task" (deterministically the cheap arm), rationale disclosing
   exactly what was scored, identical no-signal spawns sharing one router call,
   and a `systemMessage` announcing the rewrite in the TUI (`e034d86a`) — and
   then short-circuited genuinely signal-free spawns to allow-with-parent-model,
   since the `SubagentStart` audit proves spawns inherit the routed thread model,
   keeping both the chip and the audit reconciliation truthful (`a95105c9`).
   Still open (§11 row C-sub): codex's own spawn-tool naming rarely reaches
   hooks, so most spawns take the placeholder path.

### Router / seam

9. **GLM and Kimi were being stripped by our own family filter, not just
   missing upstream.** The CUJ log first recorded "GLM absent from the codex
   model list" as an external ucode/gateway distribution gap, and an earlier
   attempt at the `system.ai.glm-5-2` mapping was deliberately abandoned because
   three independent family gates — `model_catalog`, `model_override`, candidate
   filtering — rejected non-GPT ids on codex harnesses (`e034d86a`). One shared
   authority `is_codex_compatible_model` (segment-matched so lookalike endpoint
   names can't false-positive) now backs all of them plus §2's family function,
   so a codex catalog carrying `databricks-glm-5-2` keeps it through to an
   applied pick while claude harnesses still reject it. Create-time Smart Routing
   also stopped relying on §2's static tables and routes over a real pre-session
   catalog (host model-options round-trip, family filtered, static top-up only
   for harnesses that don't answer) (`158042a3`). §11 row C1 is exact as a
   result — the "external gap" was partly ours.

   **Correction (2026-08-01): the abandoned `system.ai.glm-5-2` mapping was
   right, and it is now back.** The three family gates that rejected it are
   unified behind `is_codex_compatible_model`, so the reason it was dropped no
   longer exists. Live probes on staging and prod show the Responses API serves
   GLM *only* under that name; `databricks-glm-5-2` — the endpoint the catalog
   carries — advertises chat-completions only and 400s on `/codex/v1`, and
   `system.ai.databricks-glm-5-2` 404s. GLM is in no discovery listing, so the
   name cannot be discovered, only pinned: `_SERVABLE_ALIASES` in
   `smart_routing.py` maps the bare arm `glm-5-2` to `system.ai.glm-5-2` at
   resolution time, overriding the catalog's spelling. Per-model, not a general
   prefix rule; the router's arm id stays `glm-5-2`.

10. **Two resolution authorities disagreed, and auto sessions lost their
    harness.** §2 put resolution in the seam, but the routing client had always
    resolved a pick to a servable local id internally — so `route_session_harness`
    and `route_turn` fed an already-local id back through `resolve_selection`,
    which expects *router* vocabulary. `databricks-claude-opus-4-8` matched no
    arm, the harness came out `None`, and the caller treated routing as
    unavailable: Smart Routing sessions fell back to the default harness and
    recorded turn decisions as not applied. Both callers now resolve from the
    router's own pick (`raw_model`) and `resolve_selection` maps an already-local
    id back to router vocabulary first, so the seam is idempotent from either
    direction (`972dea9d`).

11. **The workspace serves each model under two spellings.** Not anticipated
    anywhere in §1/§2: the same endpoint is listed as `system.ai.claude-opus-5`
    and `databricks-claude-opus-5`, and discovery answered with whichever listing
    happened to succeed — so a routed turn could end up with a spelling the pane
    would not accept. Discovery now unions both listings, collapses duplicates
    onto the `databricks-` spelling, and sorts versions on the bare id so a
    spelling can never outrank a version. Relatedly, prefix stripping lost its
    separator guard: a prefix configured without its trailing dot (`system.ai`)
    emitted router ids like `.claude-opus-5`; stripping now drops a leftover
    leading separator (`972dea9d`).

12. **Nearest-servable substitution was capability-blind, then still too
    aggressive.** §2 described the unservable-pick fallback as "nearest-available",
    which in practice trusted catalog list order and kept the last prefix tie —
    an alphabetical live catalog substituted `gpt-5-nano` for the codex anchor arm
    `gpt-5-6-sol`. Arms now carry tiers beside their scenario menus (cheap vs
    capable; unknown config-supplied arms default capable) and servable ids get a
    total capability ordering (size class, version, curated index) independent of
    catalog order (`dca004d8`), and cheap-tier substitution is floored at-or-below
    the arm's own class so a sonnet-class pick can never fall to haiku
    (`539b00ae`). With live pre-session catalogs (`158042a3`) the common case is
    an exact match and substitution is the exception, as §11's bar requires.

### Routing policy

13. **Per-turn re-routing was left ambiguous; it is now explicitly out.**
    §3 and the P6/P7 packets carried a per-turn routing path, and §1.1 flagged
    `session_history` as the designed hook for it. Reality: a harness pick is
    physical — a session *is* a live `claude` or `codex` process with its own
    bridge, config and pane — so re-routing turn 2 means killing a process
    mid-conversation. Recorded as §10 decisions 4 and 7: main-agent routing is
    session-start only and the top-level Smart Routing pick (harness *and* model)
    holds for the session's life.

14. **Subagent routing needed a per-call gate, not a launch-time install.**
    §5.1's relay read the enforcement decision once, at launch, which locked a
    routed session into enforcing subagent routing forever and made a mid-session
    toggle impossible. Shipped: `subagent_routing_override` (`on`/`off`/`null`)
    on the session, `null` inheriting the session-start choice; the relay
    re-reads the session (and parent) **per call**; hooks install whenever a
    server client exists so toggling on mid-session works; the change emits
    `omnigent.routing.subagent_override_changed` (`0fb7ea95`, web `1d030f22`,
    sticky per-harness default `2a415cf4`). Verified live in both directions
    (§11 rows B-tog / C-tog) — off declines per call with no decision persisted
    and the spawn proceeds.

15. **Children of a non-auto parent must be family-constrained.** §11 global
    assertion 3 said a `cc` session must never spawn a Codex arm, and the hook
    path enforced it — but `_force_auto_for_child` treated *any* routed parent as
    Smart Routing, so every child of a plain codex/claude session got
    `harness_override: auto`, was routed over a family-mixed catalog (a codex
    session producing claude-opus children), and inherited the cross-family
    escape hatch. Found live: a codex parent with nine forced-auto children.
    The auto treatment now requires the parent to actually be in auto mode,
    child routing passes the parent's family as a candidate filter, and
    `route_turn` drops out-of-family models from the self catalog (`5a397d6f`);
    auto-ness is tracked via a durable `omnigent.routing.auto_harness` label
    because the auto sentinel is consumed at first message (`0fb7ea95`). Only
    genuine Smart Routing sessions may cross families.

16. **Warning hygiene: the canary warning had to be filtered at snapshot
    build.** A consequence of delta 14 — once hooks install unconditionally, the
    `subagent_routing_unenforced` warning fired on sessions with routing off.
    The recorded observation stays durable but visibility is re-derived per
    snapshot with the same effective gate the relay applies (override, else
    own/parent cost-control), so a mid-session toggle-on reveals the warning and
    toggle-off clears it without re-posting (`5444a1a4`).

### UI / naming

17. **The name churned three times before settling.** §8 listed NewChatDialog
    reconciliation as blocked on a design call; §10 decision 2 landed on "Auto"
    after "Intelligent Routing" → "Auto Harness". It moved once more: every
    user-facing label — harness chip, dropdown item, Configure modal, the Claude
    Code / Codex Model option, the in-session subagent row, decision chip and
    card headers, the `sys_advise_models` tool title, the subagents-panel
    tooltip — now reads **Smart Routing**, with API fields, storage keys,
    sentinels and telemetry names deliberately unchanged (`e5c8a160`).

18. **Smart Routing became a persisted top-level pick, then its own picker
    group.** Not in the plan at all (§8 deferred the dialog question): an
    agentless Smart Routing session that routes harness *and* model over native
    claude/codex shipped server-side (`758bb1e8`) and web-side (`2e08a2e9`), then
    gained persistence through the same last-harness store as every other pick,
    with graceful degrade when the stored pick can't be honored — routing
    disabled or the native arm missing on this host — and an explicit harness
    click clearing the remembered sentinel (`ee26ff7c`). Finally it was lifted
    out of the Harnesses list into its own unlabeled group above it, helper blurb
    dropped, because it routes *over* the harnesses rather than being one of them
    (`76749e03`).

19. **The decision chip renders below the user message, and claude needed an
    extra rule.** Native-terminal sessions persist the routing decision *before*
    the user message, so order-faithful rendering put the chip at the top of the
    chat. Session/turn chips now attach to the message they routed — a chip whose
    following neighbor is the user message defers below it, already-correct orders
    are untouched, subagent chips never move, and streaming rebuilds the
    message+chip pair atomically in both arrival orders (`8fa280ea`). On claude
    only, the injected `/model` echo persists as a `slash_command` item *between*
    the decision and the message and broke the adjacency pairing (codex pushes via
    the app-server and emits no such item), so pairing now skips `slash_command`
    blocks in both directions (`25b75c62`).

20. **Configure Smart Routing is Permissions-only, locked to "Default".**
    §10 decision 3, shipped as a single disabled row: the picked harness inherits
    the machine's own harness defaults and the create payload carries **no**
    permission override, byte-identical to launching the harness natively; stale
    stored modes for the sentinel are no longer read (`320b6b59`). The
    cross-harness permission mapping stays deliberately deferred.

### Telemetry & PR shape

21. **Routing telemetry moved off OTel into the OSS analytics pipeline.**
    MVP requirement 4 and §7 both named `omnigent.routing.*` OTel events, and
    §3 noted the missing event-emission helper. PR review rejected the shape: a
    span-event helper in `runtime/telemetry.py` reads as debug-only and
    manufactured orphan spans when nothing was recording. Routing decisions and
    setting changes are now proper analytics events (`RoutingDecisionEvent`,
    `RoutingSettingChangedEvent`) on an allowlist posture — model ids reduce to
    family/tier labels, and rationales, prompts and task names never leave the
    transcript; enablement is state on `SessionCreatedEvent` rather than firing
    on router installation from the runner process where the analytics client
    never initializes; the parent-transcript mirror no longer double-counts; the
    OTel helper and constants are deleted (`c7f78f26`).

22. **Post-hoc de-scarring and test consolidation were needed before the PR
    split.** §4's file-ownership partition kept parallel agents from colliding
    but left visible seams. One pass deleted fourteen grep-proven dead surfaces
    and consolidated the duplication into one shared hook machinery, one
    routing-settings accessor, one family rule and one nested web payload — and
    that pass alone fixed ten latent defects, each with a regression test (codex
    fork detection, hook argparse exit-0 contract, cross-harness label agreement,
    family-filtered candidates, `fail_mode` reaching the runner hop, routing
    settings in Docker `RuntimeCaps`, bypass version-gate polarity, honest
    `raw_model` telemetry, `model_prefixes` on `RoutingSettings`, stale naming);
    production code came out net negative (`d181cbd5`). A second pass
    consolidated the four-way test sprawl behind a line-level coverage gate (zero
    lost lines python-side, zero lost statements web-side), keeping this branch's
    live-bug regression set untouched and fixing two latent test defects — a
    hardcoded advertisement filename and a shadowed telemetry wire-format test
    that never ran (`f8328623`).

### Recipe feedback (external, unresolved)

23. **task_v1's escalation rule prices well-written prompts at opus.** §11
    derives expectations from the frozen recipe, and the matrix confirms the
    recipe does what it says — which is the problem. P-OPUS escalates because it
    is clear, contained and code-referencing, so a well-written prompt *always*
    pays opus prices; and under the `both` scenario the GLM-shaped P-GLM case
    also escalates to opus rather than delegating (§11 rows A1/A2). Both are
    recorded as task_v2 feedback for the AIGW team (Ivan) per §8; task_v1 is
    frozen (§1.1) so nothing changes client-side. One related sighting was a
    misread: `"hi"` → `gpt-5-4-mini` was task_v1's own extraction self-call
    (§1.1), not a route — the route was `claude-sonnet-5` as the recipe says.

24. **Per-turn re-routing was built, live-verified, and reverted the same day.**
    Delta 13 called per-turn routing out of scope; on 2026-07-30 it was briefly
    implemented anyway (`23cfdbc2`) — a provenance gate that let a
    router-authored `model_override` stay routable (via the
    `omnigent.routing.decision` label) instead of pinning the session, plus an
    apply-skip when the new pick matched the old. It worked live, then Bryan
    ruled routing is session-start only, and the whole behavior was reverted
    (revert commit `720b145b`; docs `05a4b9e5` / `88ec745f` reverted with it).
    §10 decision 4 is the standing rule: route once, pin for the session's life.
    The revert restores the fully-verified pre-experiment state (15/15 §11
    matrix at `de2acfdb`).
