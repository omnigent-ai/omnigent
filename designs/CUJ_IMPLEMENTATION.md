# Smart Routing CUJs: end-to-end implementation walkthrough

This document records what we had to build, in pipeline order, for the three critical user journeys on `routing-mvp`. Companion documents:

- `designs/INTELLIGENT_ROUTING_PLAN.md` — the plan. Its §12 "Implementation deltas" holds the per-fix narratives that this document expands into full chains.
- `designs/CUJ_STATUS.md` — the evidence layer per CUJ, and the 14/14 matrix run.
- `designs/LIVE_MODEL_STATE.md` — the codex model-state mechanics in protocol detail.

This document cites commit shas inline, as §12 does. Those shas name the original per-fix commits, which is the granularity that the narratives need. We since rebased the branch onto `origin/main` and reconciled it with main's catalog-routing work, so those shas are no longer reachable. The shipped series is `git log --oneline origin/main..HEAD`. That series holds eleven commits. `80d3bcc7` "feat(routing): session-start smart routing core" leads the series and holds the reconciliation. Every line number below is a line number in HEAD.

This document uses six domain terms:

- **arm** — one model that the router can choose from a fixed menu.
- **seam** — the one module boundary that holds all knowledge of the router's contract.
- **pane** — the tmux pane that runs a native harness CLI.
- **rollout** — one codex turn on the running thread.
- **canary** — a hook that writes a file, plus a watcher that reports the file as absent.
- **spelling** — one of several literal id strings that name the same model, for example `system.ai.claude-opus-5` and `databricks-claude-opus-5`.

The three journeys:

1. **Claude Code CUJ.** The user opens *Configure Claude Code* on the new-chat landing. The user picks **Smart Routing** in the Model dropdown. The server creates the session with routing on and with no model pin. The router scores the first message of each turn over the Claude arms. The executor switches the claude-native terminal to the routed model before it injects the message.
2. **Codex CUJ.** The user makes the same choice in *Configure Codex*, over the Codex arms. The executor applies the routed model to the running codex thread and to that thread's on-disk mirror. It does not type the model into a pane.
3. **Smart Routing (auto) harness CUJ.** The user picks the top-level **Smart Routing** row in the harness dropdown. That row is not a harness. It is a router over the harnesses. The server chooses both the harness (claude-native or codex-native) and the model at session create, from the first message. Both choices stay for the session's life.

Reading order:

- §1 describes the shared substrate that every CUJ sits on. §2 to §4 reference §1 rather than repeat it.
- §2 and §3 describe the two apply layers. Nearly all the real work was there.
- §4 is mostly create-time composition of §1 to §3, plus its own permission rules and persistence rules.
- §5 collects the run-time behaviour that all three CUJs share.

---

## 1. Shared routing infrastructure

### 1.1 The route-options seam

`omnigent/server/smart_routing.py` holds all knowledge of the router's contract. One concrete route-options source, `TaskV1RouteOptionSource` (`:931`), holds it. `build_route_options` (`:958`) takes a harness set plus a catalog, and returns the option list that the router requires. `resolve_selection` (`:992`) takes the router's pick, and returns a `(harness, servable id)` pair.

The source always injects the frozen task_v1 arm menus (`TASK_V1_MENUS`, `:599`). It injects them even when the workspace serves no endpoint for them. task_v1 returns 400 for a partial menu, and eng-ml-inference cannot serve two of its arms (plan §1). Three callers use the source: `route_session_harness` (`:1467`), `route_turn` (`:1655`), and the runner's subagent endpoint. No caller sees router vocabulary. Each caller reaches the source through `route_option_source` (`:1162`).

The source began as a `RouteOptionSource` Protocol with one implementor, and the menus sat one level deeper under a single `router_name` key. `36a17c65` collapsed both. It merged the Protocol into its implementor. It flattened the menus into `{scenario: arms}` tables. That flattening also deleted `routing.scenario_menus`, `_parse_scenario_menus`, and the `scenario_menus` threading.

Four problems forced changes here.

**Two places resolved the pick, and the second one downgraded it.** The routing client always resolved its own pick to a servable local id. Both callers therefore passed `resolve_selection` an already-local id, such as `databricks-claude-opus-4-8`. `resolve_selection` expected router vocabulary, such as `claude-opus-4-8`. No arm matched, so `resolve_selection` returned `None` for the harness. The caller read that `None` as "routing unavailable". Smart Routing sessions then ran on the default harness without a message, and turn decisions recorded `applied=false`.

The first fix (`972dea9d`) made the seam idempotent. The seam now resolves from `RoutingResult.raw_model`, and it maps an already-local id back to router vocabulary first. The second resolution pass still remained, and that pass still lost information on the zero-config Databricks path. The server's second pass reads `routing_settings().model_prefixes`. Without a `routing:` block that value was `()`. No bare arm could then match a `databricks-` catalog id, and `databricks-gpt-5-6-luna` fell back to the cheapest model. The client now owns resolution (`36a17c65`): `route_session_harness` applies the client's `model` verbatim and derives a harness only when the client names none, and `route_turn` does not re-resolve at all.

**One prefix list, configurable, with an honest empty case.** `strip_catalog_prefix` (`:574`) removes a leftover leading separator. A prefix that a deployment configures without its trailing dot, such as `system.ai`, produced router ids like `.claude-opus-5` (`972dea9d`). The code once split the prefix list in two: the hardcoded `_BARE_ID_PREFIXES` and the configurable `model_prefixes`. `36a17c65` collapsed both onto one list, `MODEL_ID_PREFIXES` (`:571`). That one list is the default for `RoutingSettings`, for the seam, and for `ExternalRoutingClient`, so the two ends can no longer disagree. Every prefix comparison reads `routing.model_prefix`. An explicit `model_prefix: []` now means bare catalog ids, and it no longer falls back to the defaults (`46a50556`).

**Substitution for an unservable arm is a table, not a ranking engine.** The plan described the fallback as "nearest available". In practice that fallback trusted catalog list order: an alphabetical live catalog substituted `gpt-5-nano` for the codex anchor arm `gpt-5-6-sol`. The first attempt was a capability-ranking engine of about 170 lines, which held `_capability_key`, `_size_class`, `_version_key`, `_listed_rank`, and arm tiers. `36a17c65` deleted that engine and added a reviewable `{arm: (preferred, fallback, …)}` table, `_ARM_SUBSTITUTES` (`:611`). `substitute_model` (`:783`) reads that table. The ranking engine had a `_listed_rank == -1` hole: it ranked a current-generation model below everything when `MODEL_LISTS` did not list that model. The table has no such hole.

Sometimes the chain names no model on offer. `substitute_model` then takes the same-family candidate nearest to the pick's own cost position (`_cost_position`, `:757`), and on a tie it takes the cheaper candidate. The earlier fallback took the most capable same-family model, which inverted cost outright. That earlier fallback raised every SIMPLE pi turn to opus, because pi bars haiku (`46a50556`). Id comparison reads a dot as a dash (`_bare_id`, `:732`), so a picker's `gpt-5.6-sol` matches the router's `gpt-5-6-sol`. Before that change such an id matched no chain and collapsed onto the most expensive row. The offered menu now also carries one row per model instead of two. Live pre-session catalogs (§1.3) make an exact match the common case, and substitution the exception, which is what the matrix's "no fallback arrows" bar requires.

**Harness bars were unenforced on the turn path.** `_redirect_incompatible_pick` (`:898`) stays in the seam as post-verdict harness correction. The offered menu keeps the `_HARNESS_EXCLUDED_MODELS` (`:711`) pairs when the harness is itself in play, because the router needs its full menu and otherwise returns 400. The function therefore moves an incompatible pick to a harness that can run it. `46a50556` landed two corrections:

1. The redirect now takes the *offered* harness set. It declines instead of returning a harness that nobody offered. A child that its parent's family restricts can therefore no longer escape onto `codex` or `claude-sdk`.
2. A turn cannot change harness at all. A turn therefore removes the models that its own gateway bars before it offers them. When an injected arm comes back barred, the turn swaps the *model* through `substitute_model`.

`harness_bars_model` (`:884`) is the shared predicate. `designs/LIVE_MODEL_STATE.md` documents why each `pi` exclusion exists.

Post-verdict harness correction runs as **two layers, in order**, and only on the session path. Our layer runs first: `_redirect_incompatible_pick` over the static `_HARNESS_EXCLUDED_MODELS` pairs. Only this layer can swap the model instead of the harness. `_redirect_wire_incompatible_pick` (`:1428`) runs second, on whatever the first layer returns. This second layer is the catalog-driven companion, and it reads the `_RunnerModel` wire APIs that the code keeps from the live catalog (§1.3). It moves a `pi` pick to `claude-sdk` when the catalog reports that the Claude-family endpoint does not speak Anthropic Messages. This layer carries the same on-offer guard, so a family-restricted child cannot escape through it either, and it never reads unknown metadata from an older runner as incompatible. `route_turn` runs only the first layer, because a turn cannot change harness at all.

### 1.2 RoutingSettings on RuntimeCaps

`RoutingSettings` (`smart_routing.py:653`) is the frozen deployment record. It now holds three fields:

- `router_name`.
- `selection_model`. The code passes this field through as `route_selector.config.model`, so a deployment can pin an extraction model that it has query access to.
- `model_prefixes`.

The flattened menu tables (§1.1) took `scenario_menus` with them. The deleted knob and the deleted cache (§1.5) took `subagent_fail_mode` and `subagent_cache_ttl_s` with them. `36a17c65` and `6112e6cb` deleted all three fields. Every reader reads the record through one accessor, `routing_settings(caps)` (`:1129`), which returns all defaults when the caps carry no record. The de-scarring pass collapsed several ad-hoc re-parses into that accessor. The same pass also fixed Docker's `RuntimeCaps` construction, which dropped the routing settings entirely (`d181cbd5`).

`cli.py` chooses the router client at *build* time. It constructs exactly one client into `RuntimeCaps.routing_client`, so there is no runtime fallback chain. A router failure returns `None` and sets `last_error`. `routing_last_error` (`:1149`) reports that error, and the caller continues unrouted and attaches the reason (plan §2). This behaviour made the task_v1 rollback incident a logged degradation instead of an outage. `last_error` is not part of the `RoutingClient` Protocol (`:192`). The accessor always read the attribute defensively with `getattr`, so a declaration would only imply a contract that the clients did not have (`36a17c65`).

Two logging-posture corrections belong here:

1. The external router request body carries up to 4000 characters of the user's prompt. The request log therefore logs at DEBUG and replaces the prompt with its length. The INFO log keeps the router name and the route options. The judge client's raw response also logs at DEBUG (`36a17c65`).
2. The router's *rationale* paraphrases the prompt. Both entry points therefore log the rationale at DEBUG, and log the model and the harness at INFO (`46a50556`).

### 1.3 Catalogs and spelling determinism

Three catalog sources, in order of preference:

- **Live per-session:** `fetch_runner_models` (`:323`) is a thin id-only adapter over `_fetch_runner_catalog` (`:246`). `_fetch_runner_catalog` calls the runner's `/v1/sessions/{id}/models`. It keeps each row's wire APIs and cost tier on a `_RunnerModel` (`:222`). It orders the rows by cost tier, and it breaks a tie by catalog order. The post-verdict wire check reads the wire metadata that this source keeps. `catalog_models_for_harness` (`:124`) extracts the harness's slice.
- **Pre-session:** `_pre_session_model_catalog` (`server/routes/_sessions/orchestration.py:5678`) asks the host for its pre-launch model options for each candidate harness. A create has no session, so the live catalog is out of reach. The host holds the CLIs and already resolves their picker options. `158042a3` added this source, because create-time Smart Routing routed over the static tables before that commit. That is how the server offered a codex session models that the session could not run. One helper now owns the host model-options round trip for both callers, and both readers accept a picker row that spells the id as `model` or as `id` (`3b00d101`).
- **Static:** `infer_models` (`:89`) is the last resort. It serves the harnesses that the host cannot answer for. The table behind it is `MODEL_LISTS` (`:39`) plus `_CURRENT_GENERATION_MODELS` (`:68`). That table is a deliberate fork: main deleted its other uses. We keep the table here because substitution needs a cost ordering on the paths that have no catalog.

`models_in_family` (`:107`) filters the candidate set by family, whatever the source. One shared authority decides family compatibility for codex: `is_codex_compatible_model` (`model_override.py:126`). It matches each id segment, and it allows an optional trailing generation number. `system.ai.glm-5-2` and `kimi-k2-instruct` therefore pass, and a lookalike endpoint name such as `glmqlfit-eval` does not. Before that authority existed, three independent gates each rejected non-GPT ids on codex harnesses: `model_catalog`, `model_override`, and candidate filtering. We had therefore recorded GLM as an external distribution gap. The codex catalog carried `databricks-glm-5-2`, and *our own* code removed it (`158042a3`, §12 delta 9).

Spelling determinism was the other latent defect. The workspace lists the same endpoint twice, as `system.ai.claude-opus-5` and as `databricks-claude-opus-5`. `databricks_model_discovery.py` answered with whichever listing succeeded, so a routed turn could end up holding a spelling that the pane refuses. Discovery now unions both listings. It collapses duplicates onto the `databricks-` spelling. It sorts versions on the bare id, so a spelling can never outrank a version (`972dea9d`).

Discovery also returns the *full* servable Claude catalog instead of the newest model per family. The workspace kept adding newer generations, such as `claude-opus-5`, while task_v1's arms stay frozen at `claude-opus-4-8` and `claude-sonnet-5`. Newest-per-family alias pins drifted with the workspace (`af42b36c`, §12 delta 3). `discover_databricks_claude_models` survives only as a deprecation shim over the catalog lookup, and we remove that shim in v0.10.0. Its Unity Catalog short-circuit stays deleted. Unity Catalog spells every id with `system.ai.`, and a skipped gateway listing would make the catalog spelling depend on which listing answers (`3b00d101`).

### 1.4 Decision records and chip rendering

Every routing decision is a transcript item. `RoutingDecisionData` gained five fields: `harness`, `scope`, `decision_id`, `raw_model`, and `attempted_override`. `scope` is one of `session`, `turn`, `child_session`, or `native_subagent`. All five fields carry a default for legacy rows (plan §5.2). `_emit_server_routing_decision` (`server/routes/_sessions/helpers.py:5499`) writes the item, and it writes the item after the decision validates. It no longer writes before a parse failure that produces no chip (`3b00d101`). `_stamp_routing_decision_label` (`orchestration.py:4131`) writes the decision id onto the session under `ROUTING_DECISION_LABEL_KEY` (`subagent_routing.py:88`), so a reader can join a persisted `model_override` back to the decision that produced it. The `routed_model` field on a child-session row requires that label, so a user-pinned model no longer reports as routed with a null decision id (`3b00d101`).

Only one component renders a decision: `RoutingDecisionCard` in `web/src/components/blocks/StatusBlocks.tsx`, which is what `BubbleView` mounts. A second component, `RoutingDecisionChip`, sat unused for a while. `2245f57d` deleted it and moved its coverage onto the card. "Chip" below means the card in its paired position below the message.

Three rules matter for the UI, and all three exist because native sessions behave differently:

- **`applied` must be honest.** See §2.4. A decision that claims a model the process never ran is worse than a visible `applied=false`.
- **The chip renders below the user message that it routed.** A native terminal session writes the decision *before* the message, so order-faithful rendering put the chip at the top of the chat. `deferredRoutingChips` (`web/src/lib/renderItems.ts:639`) pairs a session-scoped or turn-scoped chip with the adjacent user message, and moves the chip below that message. It leaves an already-correct order untouched. It never moves a subagent chip. Streaming rebuilds the pair atomically in both arrival orders (`8fa280ea`). Only claude breaks the adjacency: on claude the injected `/model` echo persists as a `slash_command` item *between* the decision and the message, and codex sends the model over the app-server and writes no such item. `isChipPairingSkippable` (`:383`, `:390`) therefore skips `slash_command` blocks in both directions (`25b75c62`).
- **The incremental cache must count the region that it just rendered.** The same `/model` echo renders its own bubble *inside* the region between the chip and the message. The cache hardcoded two bubbles per region, so it dropped one bubble too few. It then re-sent the echo bubble on every later frame of any turn after the first turn, and it produced duplicate React keys until a full rebuild. The region now records `regionBubbleStart` (`:467`), and it reports `lastBubbleCount = bubbles.length - regionBubbleStart` (`:483`). A frame-by-frame test covers a non-zero block offset, because the cache reuses only there. Every earlier test started at block 0, where reuse bails out (`2245f57d`). The fix also split two conditions that `isChipPairingSkippable` had treated as one: "the block renders nothing", and "the block may sit between the chip and the message".

The chips earn their keep. A difference between the raw pick and the applied model caught two real apply-layer bugs.

### 1.5 The route-subagent loopback and hook machinery

A native in-harness spawn never reaches the server. Routing such a spawn therefore needs a runner-local endpoint that the harness's own hook subprocess can call. `omnigent/runner/subagent_routing.py` serves that endpoint:

- `start_subagent_router` (`:803`) binds an HTTP server on `127.0.0.1:0`, and then writes `subagent_router.json` into the session's bridge dir. That file holds `{url, token, pid, session_id, updated_at}`. It uses the same advertisement pattern as `tool_relay.json`. `SubagentRouter.close` (`:777`) deletes the file. `ensure_session_router` and `ensure_session_router_quietly` (`:1013`, `:1056`) start the router whenever a server client exists. They do *not* start it only for sessions that begin routed, so a mid-session toggle to `on` has an endpoint to call (§5).
- `resolve_subagent_route` (`:476`) holds the policy. It builds the candidate set with `candidate_models` (`:397`). It calls the router. It returns a `SubagentRouteDecision` (`:236`) of `allow`, `rewrite`, `redirect`, or `deny`, with `model`, `harness`, `raw_model`, `rationale`, and `decision_id` (plan §5.1). It denies a pick that nobody offered, because "the spawn did not run" beats "the spawn ran on the wrong model", and that case is the *only* remaining `deny`. Two callers one hop out read the enablement gate **per call**: the server relay route (`server/routes/sessions/routes_hooks.py:1388`) and the child-session path (`orchestration.py:686`). Both read it through `subagent_routing_enabled` (`:157`), which layers the per-session override over the session's own cost-control state, or over the parent's state.
- The family rules live here too: `harness_family` (`:338`), `model_in_family` (`:378`), and `auto_harness_session` (`:355`). `auto_harness_session` allows a cross-family pick *only* under the Smart Routing harness (§4.7).

We built two pieces of this layer and then deliberately deleted them (`6112e6cb`):

- **The configurable strict mode.** `subagent_fail_mode` took `open` or `closed`. `closed` was meant to make an unrouted spawn fatal, on the argument that an unrouted spawn silently voids the determinism guarantee. Every failure path already fell through to allow: no client, no candidates, a router exception, an empty verdict, a transport error, or a hook timeout. `closed` therefore could not deliver what it promised. The module docstring now documents the gate as **advisory**. `_unavailable_decision` (`:456`) is the single path that allows the spawn and states the reason. The knob, its plumbing, and the deny-on-failure branch are gone.
- **The per-`(session, task)` decision cache.** The cache saved one task_v1 extraction round trip on identical spawns. A cache hit re-sent a `decision_id` that the contract documents as an *identity*, so one decision produced duplicate transcript rows and duplicate telemetry. Correctness won.

**Hardening.** The advertisement carries a bearer token. `write_advertisement` (`:703`) therefore writes it through `os.open(..., 0o600)` into a temp file, and then moves that temp file into place with `os.replace`. The file is never world-readable, not even for the instant between a `write_text` call and a later `chmod` call. The SDK harnesses have no bridge dir of their own, so `router_dir_for_session` (`:1161`) creates a private dir for them through the shared ancestor check for bridge dirs (`ensure_secure_dir`, `claude_native_bridge.py:740`). It does not use `mkdir(mode=0o700, parents=True)`, which applies the mode to the leaf dir only, and which trusts an ancestor that already exists on the same `/tmp/omnigent-<uid>` path that the bridge hardening defends. The hook side rejects an advertisement unless it meets two conditions: the url is plain http on `127.0.0.1` or on `::1` (`_is_loopback_url`), and the advertising pid is still alive (`_advertiser_alive`). Token comparison runs in constant time. A 401 or a 404 drains the request body and closes the connection, so keep-alive cannot mis-frame the next request (`6112e6cb`).

**Lifecycle.** The router used to leak on two of the three launch paths, because only claude-native shut it down. Each leaked session cost a `ThreadingHTTPServer`, a daemon thread, a loopback socket, ledger entries, and a live token file. Shutdown now runs unconditionally, and it is idempotent. Three call sites call `shutdown_session_router` (`:1116`): both codex-native forwarder exits, and the claude-native `finally` block (`runner/native/orchestration.py:4073`, `:4124`, `:6157`). All three call it through `_shutdown_session_router_async` (`:442`), because the close joins the serving thread. For the SDK harnesses the runner's session-delete path calls it instead (`runner/app.py:3111`). `close()` deletes only an advertisement that still names its own url, because a session that forks, clears, or resumes keeps the same bridge dir, so a newer router may own the file. The router tracks every dir that it advertises into, and it prunes each one (`c46ef54d`, `6112e6cb`). `session_router_env` (`:1183`) scopes the router env vars to the launching harness, so a codex executor beneath a claude session no longer inherits the parent's session id (`6112e6cb`, `de2acfdb`).

**Timeout budget.** Four hops wait on each other. Each hop's timeout is strictly larger than the timeout of the hop that it waits on. Otherwise an inner fail-open branch can never run. The four timeouts are:

1. Harness hook — 40s.
2. Hook script `HOOK_REQUEST_TIMEOUT_S` — 30s.
3. Runner relay `RELAY_TIMEOUT_S` — 20s.
4. Server hop `SERVER_HOP_TIMEOUT_S` — 15s.

The module docstring documents these four values in one place. The values also align with the codex executor's outer timeout, which had been a dead 120s (`6112e6cb`, `de2acfdb`). A long-running session can spawn without limit, so the code caps the relay ledger (`_RELAYED_CAP`) and the agent-authored `task_name` (`_TASK_NAME_CAP`).

One shared module holds the hook scripts: `omnigent/inner/hook_scripts/subagent_router.py`. Two thin per-harness entry points call it: `claude_router_hook.py` and `codex_router_hook.py`. The module imports stdlib only, so a subprocess on the spawn path can import it. The module does five things:

1. It finds the advertisement (`discover_router_dir`, `read_router_endpoint`).
2. It reads the parent model and the terminal's model vocabulary out of `bridge.json` (`resolve_parent_model`, `resolve_model_vocabulary_env`).
3. It builds the request (`build_route_request`).
4. It calls the endpoint (`request_decision`).
5. It renders the harness's hook output (`decision_to_hook_output`, `route_pre_tool_use`).

`run_route_subagent_main` (`:642`) always exits `0`, because routing must never be the reason that a spawn fails. v1 exempts fork spawns (`FORK_SUBAGENT_TYPES`, `_FORK_SUFFIXES`).

The de-scarring pass collapsed the per-harness duplicates into this one module. The same pass fixed ten latent defects, and it added a regression test for each one. Those defects include the hook argparse exit-0 contract, codex fork detection, cross-harness label agreement, and family-filtered candidates (`d181cbd5`).

### 1.6 The enforcement canary

A hook that does not run, and that says nothing, is the worst failure mode available. The UI shows routing as on. The spawns run unrouted. Nothing reports a problem. The canary is the detector: a `SessionStart` hook writes a file into the bridge dir, and a watcher posts the session-scoped warning `subagent_routing_unenforced` (`runtime/session_warnings.py:34`) when the file is absent. We had to invert the arming logic before the canary worked; see §3.7, where the canary caught both codex apply-layer bugs. A publisher can retract the warning as well as post it; see §5.3.

### 1.7 Telemetry

Routing telemetry uses OSS analytics events, not OTel spans. There are two events: `RoutingDecisionEvent` and `RoutingSettingChangedEvent` (`omnigent/telemetry/events.py:90`, `:142`). Two functions send them: `record_routing_decision` and `record_routing_setting_changed` (`omnigent/telemetry/routing.py:41`, `:91`). The original shape was a span-event helper in `runtime/telemetry.py`. Review rejected that shape for two reasons: it read as debug-only, and it created orphan spans when nothing recorded.

The replacement takes an allowlist posture. It reduces a model id to a `model_family` label and a `model_tier` label. A rationale, a prompt, and a task name never leave the transcript. Routing enablement is state on `SessionCreatedEvent`, and not an event that the code sends when it starts the router, because the runner process starts the router and the analytics client never initializes in that process. The parent-transcript mirror no longer double-counts. `c7f78f26` deleted the OTel helper and its constants.

There is no browser-side routing telemetry. `web/src/lib/routingTelemetry.ts` once recorded `ROUTING_DISABLED_MID_SESSION` and `ROUTING_FORK_FROM_ROUTED_SESSION`. That file landed, and then it went away with both of its call sites: it carried no user-visible value, and the fork predicate was wrong. `ForkSessionDialog`'s session fetch went away with it, because it existed only to feed that event (`2245f57d`).

---

## 2. Claude Code CUJ, end to end

### 2.1 UI entry point

Smart Routing is a **Model** choice in *Configure Claude Code*. The client shows the choice only when the server sends the `smart_routing_enabled` capability, and only for `claude-code` and for `codex` (`web/src/shell/NewChatDialog.tsx`, `smartRoutingEligible` at `:2281`). The choice freezes the Effort row to an em-dash, because the router picks effort per task and a live value in that row would be wrong. The choice leaves the Permissions row alone. Plan §10 decision 1 gives the reason for the placement: routing is a property of *which model runs*, so it belongs in the Model dropdown rather than in a fourth control that a user must find. The gear tooltip mirrors the modal (`configSummary`, `:2312`, routing row at `:2341`), so a user can read the active setting without opening the modal. The tooltip also checks eligibility, so a stale `"on"` never renders a misleading routing row.

There is deliberately **no** in-session "Model = Smart Routing" toggle. Main-agent routing is a session-start concept, and an in-session switch would promise a change that cannot take effect (plan §10 decision 4). The in-session control is *Subagent routing* instead (§5.2).

### 2.2 Session create payload

The create carries `cost_control_mode_override: "on"`. It carries **no** model pin and **no** effort pin. That is the whole handshake. Routing runs only when nothing is pinned, so the missing pin is what arms routing. The `session_overrides` column on the created row is the evidence for this layer (`CUJ_STATUS.md` §1).

### 2.3 The turn gate and the routing call

`_forward_event_to_runner` (`server/routes/_sessions/orchestration.py:3675`) computes `effective_runner_override` (`:3833`). It takes the per-event override, and otherwise the persisted column. It tests with `is not None` and never with `or`, per the no-invented-defaults rule. The function then evaluates the `_should_route` gate (`:3929`), which requires four conditions:

1. Routing is enabled.
2. The event is a `message`.
3. The auto-harness block did not already route this turn.
4. No model is pinned.

In practice the gate passes on the **session's first message only**. The routed turn writes its pick as `model_override`, which is itself a pin, so turn 2 onward reuses the routed model and does not call the router.

`c2f79f1c` added a set of per-branch INFO logs that named *why* the gate declined a route: "auto-harness already routed this turn", "model already pinned (…)", and "event type … is not a message". `3b00d101` removed those logs. Each log restated the condition of the branch that held it, so each carried no information that the gate expression did not already state. The declined-route diagnostics that remain sit one level down, inside `route_turn`, where a reader cannot infer the reason from the caller. There are three such reasons: no routing client is configured, the harness has no candidate models, and the harness bars every candidate.

`route_turn` (`smart_routing.py:1655`) scores the raw user text against the `cc` scenario menu, with the candidates filtered to the Claude family. It caps the text at 4000 characters. It adds no wrapper and no summary, because `task.prompt` is the entire routing signal (plan §1.1).

### 2.4 Decision persistence and the chip

`_routed_turn_model_spelling` (`orchestration.py:3580`) answers one question *before* the server writes anything: can this pane apply the pick? A mid-session switch on a Claude pane goes in as a typed `/model` command, and that command accepts only the session's own picker vocabulary. The executor skips a routed id outside that vocabulary: it fails open, and the turn runs on the current model. The server therefore runs the *same* translation that the executor runs — `model_vocabulary_env` over the session's cached picker rows, and then `claude_model_command_arg` — and it returns `None` when the pane has no spelling for the pick. Unknown vocabulary means that no picker rows are cached yet, and in that case the function returns the routed id unchanged: the launch env is the only authority, and a guess in either direction is its own inaccuracy.

`None` now stops the *whole* downstream sequence, not only the chip. The first version of this check was `_mark_unapplied_native_turn_decision` (`af42b36c`). It only corrected the verdict to `applied=false`, and the server wrote the pin anyway. Any `model_override` blocks routing (§5.1), so one unapplicable pick disabled routing for that session permanently, and it attributed the session's usage to a model that the session never ran. `3b00d101` collapsed that marker into this pre-write check: no spelling means no `model_override` and no in-band switch, and `_unapplied_routed_verdict` (`:3629`) appends the reason to the rationale and clears `applied`. An honest `applied=false` beats a silent wrong value, and it is what makes the matrix's no-arrows bar meaningful.

The server sends the chip *after* the runner forward and after `input.consumed`, so the live SSE stream delivers the user bubble first. The client renders the chip below the message, per §1.4, including the `slash_command` skip that only claude needs.

### 2.5 The apply layer

Nearly all the work was here. Four separate problems follow, in launch order.

**Launch env pins.** claude-native launches its terminal *before* any turn decision exists, and `/model` can reach only the ids that the launch env spells. So when `launch_metadata.routing_enabled` is true, `runner/native/orchestration.py:5941` pins the family aliases at the router's frozen arms. It calls `claude_config_with_routed_arms_pinned(claude_config, task_v1_claude_arms())`. One accessor, `task_v1_claude_arms` (`smart_routing.py:640`), reads the arm list from `_TASK_V1_CLAUDE_ARMS`, and no other code duplicates that list. Without this pin, `/model opus` landed on the workspace's newest opus (`claude-opus-5`) while the chip claimed the routed arm (`claude-opus-4-8`). The workspace had moved ahead of the frozen router (`972dea9d`, §12 delta 3).

**The custom picker slot.** Claude Code has exactly one extra picker slot, and that slot takes an *exact* id. `claude_config_with_launch_model_pinned` (`claude_native.py:437`) puts the launch model in that slot when no alias spells the model. A Smart Routing create hits that case, because the harness CUJ resolves an exact model before the terminal exists (§4). The slot also gives the user a picker row to return to. The code writes both pin sets into the bridge config as `model_env` (`claude_native_bridge.py:889-895`). `MODEL_VOCABULARY_ENV_VARS` (`:891`) names the keys, and `read_model_env` (`:1067`) reads them back. The executor and the server do not share the terminal's environment, and both need to know its vocabulary.

**The vocabulary itself.** `omnigent/claude_model_vocabulary.py` is the shared authority. It imports stdlib only, so a hook subprocess can import it on the spawn path. We learned its premise the hard way: Claude's model vocabulary is *closed*. The `model` parameter of the `Agent` tool and the `Task` tool is an alias enum (`sonnet`, `opus`, `haiku`, `fable`), so a catalog id fails schema validation and the spawn dies before it starts. `/model` accepts three things: an alias, the byte-exact `ANTHROPIC_CUSTOM_MODEL_OPTION` value, or an arbitrary id. It accepts an arbitrary id only when a live one-token endpoint probe succeeds mid-turn. A servable catalog id is therefore *not* a spelling that the harness accepts, and the plan's single-step `resolve_selection` had assumed that it is.

The module does three things. It inverts the `ANTHROPIC_DEFAULT_*_MODEL` pins (`alias_pins`). It rebuilds a vocabulary from picker rows (`model_vocabulary_env`). It translates a model id with two functions: `claude_model_alias` (`:132`) for the Agent enum, and `claude_model_command_arg` (`:176`) for `/model`. `claude_model_command_arg` also accepts the custom slot's exact id. `normalized_model_id` (`:68`) sits in the same file and produces the comparison spelling, and the codex spawn audit reuses it (§3.7), so a prefix difference or a case difference never reads as a different model. Translation requires an **exact** pin match. A family segment alone is not enough: with `opus` pinned to `claude-opus-5`, the alias would run a model that nobody routed to while the record claimed the routed one. Both functions fail open, and `None` means "leave the model alone" (`539b00ae`, `af42b36c`).

**The routed model never reached the executor.** The plan listed native `/model` injection as existing capability, and it treated the act of applying a routed model as solved plumbing. In reality the runner's `_run_turn_bg` (`omnigent/runner/app.py`) rebuilt the harness request field by field, and it never copied `model_override` off the incoming message. The executor's injection branch therefore never ran, and a routed session kept its launch model. `/effort` is a separate session change, and `/effort` worked, which is exactly why nobody noticed. The code now forwards the field explicitly, and it logs at INFO on every hop: server forward, runner intake, turn dispatch, and the executor's type-or-skip-with-reason step. The chain therefore cannot go silent again (`82cac6fa`). The field reaches the executor from the runner as `ExecutorConfig.model` (`runtime/harnesses/_executor_adapter.py:285`).

**The switch itself.** `ClaudeNativeExecutor.run_turn` (`inner/claude_native_executor.py:107`, switch-and-inject at `:155-195`) applies the switch and injects the message as one step under `_inject_lock`. `inject_slash_command("/model <arg>", auto_confirm=True)` runs to completion first, and `inject_user_message` runs next. One lock over both steps removed a second writer on the same tmux pane. Routing used to switch the model with a separate `model_change` event that the server sent, and that event raced the inject, so the message's keystrokes could land in the middle of the switch and disappear. That race was the "routing drops the first message" bug. `_model_command_arg` (`:196`) types the command only when two conditions hold. First, `_should_switch_model` (`:253`) reports that the pane does not already run the model; that check seeds its baseline from the spawn `launch_model`, so turn 1 compares against the model that Claude actually booted with. Second, the vocabulary translation succeeds. Every skip logs its reason.

### 2.6 Live-state visibility

The switch goes through `/model`, so the harness's own UI shows it. The pane echoes the command, and the pane prints the new model banner. That banner is exactly the process-truth handle that the matrix reads (`tmux capture-pane`, plan §11.2). Claude Code writes that pick as the machine's default model, so a routed session leaves the user's next manual `claude` launch on the routed arm. We accept that behaviour for the MVP, because the Model picker in the harness config modal already behaves the same way (plan §10 decision 8).

### 2.7 Subagent routing

`build_hook_settings` (`claude_native_bridge.py:1188`) registers `claude_router_hook` as a `PreToolUse` hook on the agent-tool matcher. That matcher is `AGENT_TOOL_MATCHER` (`hook_scripts/subagent_router.py:59`), and the registration sits at `:1441`. A settings-level hook recurses into nested subagents. The hook rewrites `tool_input.model` through `hookSpecificOutput.updatedInput` with `permissionDecision: "allow"`, or the hook denies the spawn. The Agent tool's `model` is a closed enum, so the hook translates the id through `claude_model_alias`, and it reads the vocabulary out of `bridge.json` (`claude_model_translator`, `hook_scripts/subagent_router.py:425`). That translation turned a 7 ms schema failure into a spawn that ran to completion on the routed arm (`CUJ_STATUS.md` §4). The code filters the candidates by family, so a `cc` session can never spawn a Codex arm. §5.2 covers mid-session toggling.

### 2.8 Warnings and telemetry

Claude's hooks are settings-level, and Claude cannot mark them untrusted, so there is no canary path here. Three signals stay visible: the decision chips, the per-subagent model in the sub-agents panel, and one `RoutingDecisionEvent` per decision (§1.7).

---

## 3. Codex CUJ, end to end

### 3.1 UI entry point

The entry point is identical. Smart Routing sits in the Model row of *Configure Codex*, behind the same eligibility check. Codex folds routing into its Model row and shows no separate toggle, so the gear tooltip reports routing the same way that Claude's tooltip reports it (`NewChatDialog.tsx:2341`).

### 3.2–3.4 Create, gate, decision

Codex uses the same create payload (`cost_control_mode_override: "on"`, no pin). It uses the same `_should_route` gate. It uses the same `route_turn` call, over the `codex` scenario menu, which holds all three Codex arms. `is_codex_compatible_model` filters the candidates, and that filter is what lets `databricks-glm-5-2` reach an applied pick (§1.3). Chip pairing needs no `slash_command` skip here: codex sends the model over the app-server and writes no transcript item for it.

### 3.5 The apply layer

**Three writers** apply a model to codex. Neither §2 nor §3 of the plan expected three, and the three fought each other:

1. `thread/settings/update` switches the running thread. It switches only the thread, and it does not write `config.toml`.
2. The top-level `model` key in the per-session `config.toml`. An in-TUI `/model` writes that key, and two omnigent readers read it: the forwarder's mirror and the cost-gate hook.
3. The launch pin `_pin_codex_config_model` (`codex_native_app_server.py:204`) seeds that key, and the launch command passes the same value to the TUI as `-c model="…"`.

The observed symptom was that the routed model survived exactly one turn. The sequence ran as follows:

1. The router routed turn N.
2. The executor sent the model to the thread.
3. The rollout genuinely ran the routed model.
4. The forwarder's `turn/started` handler then re-read the **stale** `config.toml`.
5. The handler posted `external_model_change(launch default)`.
6. The server wrote that value as `model_override`.
7. Turn N+1 skipped routing, because a model was already pinned.
8. Turn N+1 sent the launch default back onto the thread.

Every surface settled back on the launch model. `designs/LIVE_MODEL_STATE.md` holds the full trace and the protocol probes behind it.

What landed (`0fcc313f`, `51801530`):

- **First-turn send under the inject lock.** The forwarded message carries `model_override` in band. `CodexNativeExecutor.run_turn` sends `thread/settings/update` before the bare `turn/start`, under `_inject_lock`, which is the same switch-then-inject discipline that claude-native uses. This design also closes the launch race by construction: a terminal that the code auto-creates at session bind starts before the first message, so no re-read timing could help, but every turn re-applies `ExecutorConfig.model` and the thread converges on the routed model at turn 1.
- **A `config.toml` mirror on a successful switch.** `write_codex_config_model` (`codex_native_bridge.py:345`) writes the same key that the TUI's `/model` writes, so the cost gate and the mirror agree instead of diverging.
- **Forwarder precedence** (`codex_native_forwarder.py:2737`, `_refresh_model_from_config`). The state tracks two values. `settings_model` holds the last live `thread/settings/updated` value, which is the running thread's truth. `last_config_model` holds the last value read from `config.toml`. A config value that *changed* since the previous read wins, because that change is either a genuine in-TUI `/model` or our own mirror write. An unchanged config value loses to the model that the executor sent. This rule keeps the routed model even when the mirror write fails, and it still obeys a user's in-TUI switch. `_sync_model_change` (`:2774`) posts `external_model_change` only on a real difference, and the server de-duplicates against `conv.model_override`, so there is no echo loop.
- **A `session.model` SSE event when the server writes the routing decision.** `_publish_routed_model` (`orchestration.py:3646`) sends it, so the web dropdown tracks live state instead of waiting for a reload. The event carries the spelling that the session's picker uses, which is a tier alias and not a catalog id, because that is what the dropdown matches against. The native path sends picker vocabulary too (`3b00d101`).

### 3.6 Live-state visibility

We probed codex-cli 0.145.0 live. `thread/settings/update` requires the `experimentalApi` capability, which we already send. It emits `thread/settings/updated`. The app-server broadcasts that notification to other clients that resumed the thread, so the `--remote` TUI receives it and its **bottom status bar updates immediately**. The startup banner box is static, and the `/model` picker does not highlight a model outside its own catalog. Both behaviours belong to the upstream TUI, and we record them rather than work around them. The reversion loop is now fixed, so the thread genuinely stays on the routed model, and `/status`, the status bar, and a resumed TUI all agree.

### 3.7 Subagent routing

Codex needs the most machinery of the three harnesses, and a live failure forced every piece of it.

**Hook generation and merge.** `codex_router_hooks_settings` (`inner/codex_executor.py:876`) builds the Omnigent half of a `hooks.json`. That half holds three hooks: a `PreToolUse` gate on the spawn tool, a `SessionStart` canary, and a `SubagentStart` audit writer. The spawn matcher is the regex `.*spawn_agent` (`_CODEX_SPAWN_AGENT_MATCHER`, `:824`), because codex flattens the tool name to `collaborationspawn_agent` on 0.145.x. On the SDK executor path, `write_codex_router_hooks_file` (`:1026`) merges that half with the user's hooks. On the app-server path, `_write_codex_policy_hooks_file` (`codex_native_app_server.py:1008`) merges the policy hooks, the routing hooks, and the user's hooks.

**One writer, one file, and probe the version first.** Subagent routing on codex earlier than 0.129 used to *delete the user's hooks*. `_populate_codex_home_config` deleted the symlink to `~/.codex/hooks.json`, because the generated file was going to take that name. Only afterwards did the version check decide to write no file, and the private `CODEX_HOME` then held no `hooks.json` at all. The code now probes the version before it populates the home dir (`app_server.py:628-631`), so an unsupported codex keeps the symlink. The root cause was two divergent `hooks.json` writers, and whichever writer ran last erased the other writer's contribution. Both now call one shared function, `write_codex_hooks_file`, which takes a *list* of payloads — the policy payload, the routing payload, and the user's own — and merges them into a single atomic write (`c46ef54d`).

**`--dangerously-bypass-hook-trust` is a no-op for app-server-dispatched hooks.** The plan recorded the bypass flag as existing groundwork, and it assumed that the flag handled the trust gate. A live probe matrix showed a different result: the generated routing hooks stayed untrusted and codex *silently skipped* them, while the policy hooks worked, because the code had only ever written the policy module's hashes. Both app-server launch paths now run a trust handshake for the router hook module and write the result. The handshake has three steps:

1. Call `hooks/list`.
2. Call `config/batchWrite` to set `hooks.state.<key>.trusted_hash = currentHash` (`_persist_hook_trust`, `:1157`).
3. Call `hooks/list` again to check the result (`trust_codex_router_hooks`, `:1194`; the policy equivalent is `trust_native_policy_hooks`, `:1257`).

Both paths run the handshake immediately after the app-server connects (`:774-781`). Both filter by hook module, so the trust step never touches a hook that the user's own file contributed. The handshake is best-effort and isolated, so a routing-trust failure can never disable the policy gate (`e32c4925`). The flag survives only where it actually works, which is the interactive TUI launch (`_CODEX_BYPASS_HOOK_TRUST_FLAG`, `codex_native_app_server.py:1997`), and nothing on the app-server path depends on it. Both paths treat a codex version that we cannot parse as *supported*, so a flaky probe can never wedge a terminal on a prompt that no subagent can answer (`c46ef54d`).

**`python -I`.** The plan described the hook scripts as "pure functions around the endpoint call", and it gave no thought to how a process imports them. Codex runs a hook command with the *session workspace* as the cwd, and `python -m` puts the cwd first on `sys.path`. A workspace that holds an `omnigent/` directory therefore shadowed the installed package, and this repo is the single most likely such workspace. Every generated hook then died on import, and none of them said so: the routing gate, the canary, the spawn audit, and the policy hook alike. `_codex_router_hook_command` (`:833`) now runs `python -I -m …`, which matches the bridge MCP command's posture. A subprocess regression test runs the real canary from a workspace that holds a decoy package (`518376ba`).

**The canary was a circular detector.** As we first built it, the watcher read the relay ledger that the broken hooks would have written, so a *total* hook failure looked like silence. The watcher now arms on the router advertisement (`subagent_routing_armed`, `codex_native_forwarder.py:5748`), and it anchors on the first turn, because codex sends `sessionStart` at the first turn and not at thread start. `_watch_subagent_routing_enforcement` (`:5809`) posts `subagent_routing_unenforced` within one tick when the canary file is absent (`e32c4925`). cwd shadowing proved that a second failure mode exists, so the message now reads "untrusted, or the hook command failed" (`518376ba`). Teardown cancels the watcher task, so a session that never takes a turn cannot leak it (`c46ef54d`). This watcher caught both codex bugs. `reconcile_spawn_audit` (`codex_executor.py:1153`) also compares the actual `model` in the `SubagentStart` audit against the models that we routed to, and it compares through `normalized_model_id`, because codex reports its own spelling and a prefix difference or a case difference is not a different model (`c46ef54d`).

**Encrypted spawn payloads and the no-signal path.** Codex encrypts the spawn `message` in a hook payload, so routing must work from `task_name` plus metadata. The plan knew that, but it assumed that a name is always present. A live spawn frequently carries no task name and no agent name, and an empty task produced a router 400 that the chip reported as a router outage. The first answer matched ucode PR 251 (`e034d86a`), and it did four things:

1. It routed an unnamed spawn on the fixed placeholder task `"Codex subagent task"`. That task is short and holds no code, so it deterministically chose the cheap arm.
2. It disclosed exactly what the router scored.
3. It shared one router call across identical no-signal spawns.
4. It announced the rewrite in the TUI through a `systemMessage` (`with_system_message`, `hook_scripts/codex_router_hook.py:96`, which is still the codex hook's post-processor).

Two later commits replaced that answer. `a95105c9` short-circuited a signal-free spawn to allow-with-parent-model, because the `SubagentStart` audit proves that a spawn inherits the routed thread model, which keeps both the chip and the audit reconciliation truthful. `6112e6cb` then deleted the placeholder task and its disclosure marker outright. `_routing_task` (`subagent_routing.py:441`) returns `None` when there is no signal, and `_decide` (`:533`) allows the spawn unchanged on `req.parent_model`, with the rationale "No routable signal (encrypted prompt, no task name); subagent inherits the session model". No code scores a synthetic prompt any more, so no code has to disclose one.

### 3.8 Warnings and telemetry

The server delivers `subagent_routing_unenforced` on the session-status channel, and the client renders it as a session warning banner. §5.3 holds its visibility rule. Decision telemetry uses the shared path (§1.7).

---

## 4. Smart Routing (auto) harness CUJ, end to end

### 4.1 UI entry point

Smart Routing is its own **unlabeled dropdown group above** the Harnesses list (`NewChatDialog.tsx:1160-1175`). `76749e03` deleted the helper blurb: Smart Routing routes *over* the harnesses, and it is not one of them. The label changed three times — "Intelligent Routing" → "Auto Harness" → "Auto" → **Smart Routing** — and the final rename (`e5c8a160`) swept every user-facing surface: the harness chip, the dropdown item, the Configure modal, the Claude Code and Codex Model option, the in-session subagent row, the decision chip and card headers, the `sys_advise_models` tool title, and the subagents-panel tooltip. The rename deliberately left the API fields, the storage keys, the sentinels, and the telemetry names unchanged. The labels live in `web/src/lib/agentLabels.ts` (`SMART_ROUTING_LABEL`, `AUTO_HARNESS_ID` = `"auto"`, `AUTO_NATIVE_HARNESS_ID` = `"auto-native"`).

**Persistence and degrade.** The client remembers the pick in the same last-harness store that it uses for every other harness (`handleSelectSmartRoutingHarness`, `:2987`), and it stores the pick under the placeholder wrapper agent's id as `AUTO_NATIVE_HARNESS_ID`. The client cannot use a restored sentinel in two cases: routing is disabled, or this host has no native arm. In either case the client falls back to the default pick. A click on the placeholder wrapper's own row clears the remembered sentinel, so the explicit choice is what survives a reload (`ee26ff7c`). The landing's "Smart Routing dropped" notice was itself wrong for a while: it always blamed host readiness, it appeared for a `localStorage` pick that was never available during that visit, and it stacked with the harness-readiness notice. The client now derives the cause and quotes it, the notice now requires a loss of availability *while the landing is open*, and the readiness notice wins the slot (`2245f57d`, `9c81bbb8`).

**Configure Smart Routing is Permissions-only, locked to a disabled "Default".** The modal shows no Model row and no Effort row, because the router owns both. The create payload carries **no** permission override at all, so the chosen harness inherits the machine's own defaults, byte-identical to a native launch of that harness. The client no longer reads a stale stored mode for the sentinel (`320b6b59`). We researched a cross-harness permission mapping between the Claude modes and codex `approval_policy` × `sandbox` × profiles, and we then deliberately deferred it. The four-way mapping holds enough asymmetry that a wrong version would loosen sandboxing without a message. The disabled row keeps the slot visible until that mapping lands (plan §10 decision 3).

### 4.2 The create payload

Two fields do the work. The first is `harness_override: "auto"`. The second is `smart_routing_message`, which carries the user's first-message text (`server/schemas.py:1380`, sent at `NewChatDialog.tsx:3236`). That field carries the text as the client *delivers* it, which means the mention preamble plus sanitization. It does not carry the raw box contents, so the router scores what the harness will actually see (`2245f57d`). The create needs a concrete `agent_id`, so the client binds the Claude native wrapper as a *placeholder*. The picker hides that row's highlight while the sentinel is active, so the row does not look like a Claude Code pick.

### 4.3 Create-time routing over native harnesses

A native session's harness cannot wait for the first message. The bundle-agent auto path can wait, but a native terminal launches as soon as the session row exists. `_resolve_native_smart_routing` (`orchestration.py:5707`) therefore routes at create time, in five steps:

1. It authorizes the caller's `host_id` (`resolve_host_owner`, `_host_launch.py:49`, called at `:5752`).
2. It reads the host.
3. It filters `AUTO_NATIVE_ROUTING_HARNESSES` (`smart_routing.py:1413`) down to the CLIs that the host actually installs (`_installed_native_harnesses`, `:5595`).
4. It calls `route_session_harness` over the `both` five-arm menu, with candidates from `_pre_session_model_catalog` (§1.3).
5. It returns the chosen native **wrapper agent name**.

The caller rebinds `agent` to that wrapper (`:5856-5880`). From that point the create is byte-identical to a normal native create, including the terminal launch, and nothing launches twice. The caller passes the routed model into `validate_session_model_metadata` (`:5899`) as the session's `model_override`, so the model reaches the CLI as a `--model` argv element at launch. `--model` is a different contract from `/model`, and a more permissive one: `--model` takes any string verbatim. This is why the harness CUJ needs the custom picker slot (§2.5): the session boots on an exact id that no alias spells.

The order of the host authorization is not incidental. As we first wrote this function, it read the host's harness readiness and sent `HostModelOptionsFrame`s over the host's live connection, and it did both about 150 lines *before* `_validate_session_workspace` authorized the caller. That order leaked the presence of a CLI and of a catalog on a foreign host, and it delivered frames into another user's host connection. `resolve_host_owner` now runs first (`3b00d101`).

This path writes `harness_override` as `None` rather than `"auto"`. A native wrapper rejects a harness override, and a sentinel left behind would make the first message re-route a terminal that already runs. The code instead writes a durable label, `omnigent.routing.auto_harness` (`AUTO_HARNESS_LABEL_KEY`, `subagent_routing.py:94`). The first message consumes the sentinel, so nothing else would survive to answer one question: was this session genuinely Smart Routing? (`0fb7ea95`).

An unavailable router does not fail the create. The create lands on the first installed CLI with that CLI's own default model, and it returns an `error` string that the routing card shows. `_resolve_native_smart_routing` returns `None` for the agent only when the host installs no native CLI at all, which is a hard 400.

### 4.4 The double-resolution fix

This CUJ is where the seam bug of §1.1 was fatal rather than cosmetic. `route_session_harness` passed the client's already-local pick back through `resolve_selection`. No arm matched, so `resolve_selection` returned `None` for the harness, and the create used the fallback harness without a message. That failure is the "auto sessions lost their harness" failure. `972dea9d` fixed it by resolving from `raw_model` and making `resolve_selection` idempotent. `36a17c65` then removed the second resolution pass entirely, so `route_session_harness` applies the client's `model` verbatim and derives a harness only when the client names none. The matrix re-run on 2026-07-30 closed rows A1–A4 on the first fix: the panes showed Opus 4.8, and the log held zero `harness=None` warnings.

### 4.5 Decision, chip and live state

The server writes one **session**-scope decision, and that decision carries both the harness and the model. The client renders it as the decision card under the first user message, per §1.4. After that the session behaves exactly as §2 or §3 describes, and the winning arm decides which one. A later change uses the codex thread send or the claude `/model` path.

### 4.6 No re-routing after session start

Turn 2 must not produce a second session-scope decision. The harness pick is *physical*: a session is a live `claude` process or a live `codex` process, with its own bridge, config, and pane. A re-route on turn 2 therefore means that the code kills and relaunches a process in the middle of a conversation. Plan §10 decisions 4 and 7 record this rule. Per-turn harness routing waits on the router's unused `session_history` field.

### 4.7 Cross-harness subagents, only here

A spawn under a genuine Smart Routing session may pick either family. A spawn anywhere else may not, and the naive version of this rule was a live bug. `_force_auto_for_child` treated *any* routed parent as Smart Routing, so every child of a plain codex session or a plain claude session got `harness_override: "auto"`. The router then routed that child over a family-mixed catalog, and the child inherited the cross-family escape hatch. We found the bug live: one codex parent had nine forced-auto children, and some of them ran claude-opus.

`5a397d6f` made three changes. The auto treatment now requires the parent to actually run in auto mode (`auto_harness_session`, checked at `orchestration.py:5935`). Child routing now passes the parent's family as a candidate filter (`allowed_family`, `:3951-3965`). `route_turn` now removes out-of-family models from the self catalog. `46a50556` closed the last escape: the post-verdict harness redirect (§1.1) used to return `codex` or `claude-sdk` whether or not the caller offered them, so a family-restricted child could still land outside its family. The redirect now declines unless the caller offered the replacement, and it swaps the model instead. The same family rule backs the hook path for in-harness spawns, so a native spawn and an omnigent child session cannot disagree.

### 4.8 Warnings and telemetry

Telemetry is the same as §1.7. The routing card's `error` string is the only auto-specific surface: a degraded create explains itself through that string ("Routing unavailable; using the default native harness.").

---

## 5. What all three CUJs share at run time

### 5.1 Session-start routing, then session-pinned

Routing runs **once per session**, on the session's first message, and the model that routing picks stays for the session's life. There is no per-turn re-routing. `_should_route` requires that no effective override exists, and the routed turn itself writes `model_override`, so the pin that the router installs is what stops turn 2 from routing again. For the Smart Routing harness the code writes the same pin at create time instead. A manual pick blocks routing by the identical rule: any `model_override` is an effective override. The per-branch "not routed because X" INFO logs first made this behaviour legible, and they let us read the codex reversion loop as "model already pinned" rather than "routing broken". We removed those logs once the gate expression said the same thing on its own (§2.3).

### 5.2 Subagent routing: inherit, override, per-call

`subagent_routing_override` on the session takes `"on"`, `"off"`, or `null`, and `null` inherits the session-start choice: a Smart Routing main agent routes its subagents, and a manually pinned main agent does not. The in-session gear row shows this control for Claude Code sessions, for Codex sessions (native and SDK), and for Smart Routing sessions. A user can toggle it at any time, and the new value takes effect on the next spawn (`0fb7ea95`, web `1d030f22`, sticky per-harness default `2a415cf4`).

**"Inherit" is its own option** in that row (`web/src/pages/ChatPage.tsx:5673`, `:5781`). The row used to collapse that option onto the effective `on` or `off`, and that collapse broke the row in two ways. Radix sends no `onValueChange` for the value that it already displays, so a second pick of the inherited value wrote nothing. The row also labelled the option "Default" for sessions that the spec routes by default (`2245f57d`).

The original design read the enforcement decision once, at launch, so a routed session enforced subagent routing forever and a mid-session toggle could not work. Two changes make the toggle work:

1. The code re-reads the enablement gate **per call** on the way in (`subagent_routing_enabled`, §1.5).
2. The code installs the hooks whenever a server client exists. It no longer installs them only for a session that starts routed, so a mid-session toggle to `on` has an endpoint to reach.

We checked both directions live on both harnesses (matrix rows B-tog and C-tog). With the toggle off, the gate declines per call, the server writes no decision, and the spawn proceeds. With the toggle on, the router routes the very next spawn. A toggle sends `RoutingSettingChangedEvent`.

### 5.3 Warning hygiene

The code now installs the hooks unconditionally, so the canary posts its warning on a session that has routing *off*, which is a direct consequence of the previous change. The recorded observation stays durable. Each session-snapshot build derives visibility again, through the same effective gate that the relay applies: the override, and otherwise the session's own cost-control state or the parent's state (`orchestration.py:686-696`). A mid-session toggle to `on` therefore shows the warning, and a toggle to `off` hides it, and neither toggle posts the warning again (`5444a1a4`).

Three follow-on changes made the banner behave:

- **Warnings are clearable.** Two call sites call `session_warnings.clear(session_id, codes=None)` (`runtime/session_warnings.py:123`). A publisher that posts an empty list calls it, scoped to `EXTERNAL_WARNING_CODES`, which are the codes that this publisher's own check covers (`routes_events.py:757`). Session delete calls it unscoped (`:1704`), because the session is gone and every code goes with it. The relay path does *not* clear: a relayed spawn proves only that one hook ran, and the blanket clear there wiped exactly the warnings that the publisher had just raised (`routes_hooks.py:1371-1377`), so the canary watcher owns the repair and posts the warning on its next check. The code allowlists the codes and reduces the payload to known string fields, so the index cannot grow arbitrary shapes (`3b00d101`, empty-list clearing `c46ef54d`).
- **The banner can appear without a reload.** The server records a warning while the session runs, and warnings have no event channel of their own, so the web client polls the open session's snapshot. That poll first asked for `refresh_state=true` on *every* fetch, which dropped the runner's skills cache and model-options cache twice a minute, per open session, forever. Only the cache-cold read refreshes now, and the poll stops after two consecutive 404s (`2245f57d`, `9c81bbb8`).
- **An unknown code cannot break the header.** The copy table in `SessionWarningBanner` is a `Map` (`web/src/shell/SessionWarningBanner.tsx:26`), and not a plain record. A wire code that names an `Object.prototype` member, such as `__proto__` or `toString`, passed the old filter and then threw during rendering (`9c81bbb8`).

### 5.4 Fail-open, with the reason attached

Every path fails open. A router outage leaves the turn unrouted, and the UI shows `last_error`. The task_v1 rollback incident proves that behaviour: the 400s produced turns that ran unrouted and logged the reason, rather than broken turns.

The spawn path was *meant* to be the exception. A configurable `subagent_fail_mode` with the setting `closed` would refuse the spawn, on the argument that an unrouted spawn silently voids the determinism guarantee. Every failure branch already allowed the spawn, so the knob promised enforcement that it never delivered. `6112e6cb` deleted the knob, and the module docstring of `subagent_routing` now documents the gate as advisory (§1.5).

The one surviving `deny` is a router pick outside the offered menu, which is a *wrong* answer rather than a missing one. In every case the decision record is the only durable log of a routing decision, because AIGW has no server-side decision logging yet. An honest `applied` value (§2.4) and a visible difference between the raw pick and the applied model on the chip (§1.4) are therefore load-bearing rather than cosmetic.

---

## 6. Known-open items

- **Codex spawn naming rarely reaches the hooks.** Most codex spawns therefore carry no routable signal at all, and the gate allows them through on the parent's model rather than routes them (matrix row C-sub). The routing gate is real on those spawns, but it has nothing to score.
- **task_v1 prices a well-written prompt at opus.** P-OPUS escalates because it is clear, contained, and code-referencing. Under the `both` scenario the GLM-shaped case escalates too, rather than delegates. The recipe does what it says. The recipe is frozen, so this item is task_v2 feedback for the AIGW team, and not a client-side change.
- **Cross-harness permission mapping** stays deferred. Configure Smart Routing keeps its disabled Permissions row as the slot for it.
- **Fork spawns** are exempt from routing in v1. Tests pin that exemption.

