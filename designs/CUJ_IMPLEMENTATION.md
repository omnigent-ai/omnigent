# Smart Routing CUJs: end-to-end implementation walkthrough

What we actually had to build, in pipeline order, for the three critical user
journeys on `routing-mvp`. Companion documents:
`designs/INTELLIGENT_ROUTING_PLAN.md` (the plan, plus §12 "Implementation
deltas" — the per-fix narratives this doc expands into full chains),
`designs/CUJ_STATUS.md` (evidence layer per CUJ and the 14/14 matrix run) and
`designs/LIVE_MODEL_STATE.md` (codex model-state mechanics in protocol detail).
Commit shas are cited inline, as in §12.

The three journeys:

1. **Claude Code CUJ.** In *Configure Claude Code* on the new-chat landing the
   user picks **Smart Routing** in the Model dropdown. The session is created
   with routing on and no model pin; each first message of a turn is scored by
   the router over the Claude arms, and the claude-native terminal is switched
   to the routed model before the message is injected.
2. **Codex CUJ.** The same choice in *Configure Codex*, over the Codex arms.
   The routed model is applied to the running codex thread (and its on-disk
   mirror) rather than typed into a pane.
3. **Smart Routing (auto) harness CUJ.** The user picks the top-level **Smart
   Routing** row in the harness dropdown — not a harness at all but a router
   over them. Harness (claude-native vs codex-native) *and* model are chosen at
   session create, from the first message, and hold for the session's life.

Reading order: §1 is the shared substrate every CUJ sits on and is referenced
rather than repeated by §2–§4. §2 and §3 are the two apply layers, which is
where nearly all the real work was. §4 is mostly create-time composition of
§1–§3 plus its own permission and persistence rules. §5 collects the run-time
behaviour all three share.

---

## 1. Shared routing infrastructure

### 1.1 The route-options seam

All knowledge of the router's contract lives in `omnigent/server/smart_routing.py`
behind one concrete route-options source, `TaskV1RouteOptionSource` (`:859`):
`build_route_options` (`:886`) turns a harness set plus a catalog into the option
list the router requires, and `resolve_selection` (`:920`) turns the router's
pick back into a `(harness, servable id)` pair. It injects the frozen task_v1 arm
menus (`TASK_V1_MENUS`, `:527`) even when the workspace serves no endpoint for
them, because task_v1 400s on a partial menu and two of its arms are unservable
on eng-ml-inference (plan §1). Callers — `route_session_harness` (`:1354`),
`route_turn` (`:1525`), and the runner's subagent endpoint — never see router
vocabulary; they reach the source through `route_option_source` (`:1090`).

It was originally a `RouteOptionSource` Protocol with one implementor, and the
menus were nested one level deeper under a single `router_name` key. Both
collapsed in `36a17c65`: the Protocol into its implementor, the nesting into flat
`{scenario: arms}` tables — which took `routing.scenario_menus`,
`_parse_scenario_menus` and the `scenario_menus` threading with it.

Four things forced changes here.

**Two places resolved the pick, and the second one downgraded it.** The routing
client had always resolved its pick to a servable local id internally, so both
callers were feeding `resolve_selection` an *already-local* id
(`databricks-claude-opus-4-8`) where it expected router vocabulary
(`claude-opus-4-8`). No arm matched, the harness came out `None`, and the caller
read that as "routing unavailable": Smart Routing sessions silently fell back to
the default harness and turn decisions recorded `applied=false`. The first fix
made the seam idempotent — resolve from `RoutingResult.raw_model` and map an
already-local id back to router vocabulary first (`972dea9d`) — but the
re-resolution itself remained, and on the zero-config Databricks path it was
still lossy: the server's second pass reads `routing_settings().model_prefixes`,
which was `()` with no `routing:` block, so no bare arm could match a
`databricks-` catalog id and `databricks-gpt-5-6-luna` collapsed to the cheapest
model. So the client now *owns* resolution: `route_session_harness` applies its
`model` verbatim and only derives a harness when the client named none, and
`route_turn` no longer re-resolves at all (`36a17c65`).

**One prefix list, configurable, with an honest empty case.**
`strip_catalog_prefix` (`:502`) drops a leftover leading separator, because a
prefix configured without its trailing dot (`system.ai`) produced router ids like
`.claude-opus-5` (`972dea9d`). The hardcoded `_BARE_ID_PREFIXES` /
configurable-`model_prefixes` split then collapsed onto one `MODEL_ID_PREFIXES`
(`:499`), the default for `RoutingSettings`, the seam and `ExternalRoutingClient`
alike, so the two ends can no longer disagree (`36a17c65`). Prefix comparisons
honour `routing.model_prefix` throughout, and an explicit `model_prefix: []` now
means bare catalog ids rather than silently falling back to the defaults
(`46a50556`).

**Substitution for an unservable arm is a table, not a ranking engine.** The plan
described the fallback as "nearest available", which in practice trusted catalog
list order: an alphabetical live catalog substituted `gpt-5-nano` for the codex
anchor arm `gpt-5-6-sol`. The first attempt was a ~170-line capability-ranking
engine (`_capability_key`, `_size_class`, `_version_key`, `_listed_rank`, arm
tiers) — deleted in `36a17c65` for a reviewable `{arm: (preferred, fallback, …)}`
table, `_ARM_SUBSTITUTES` (`:539`), read by `substitute_model` (`:711`). The
ranking engine had a `_listed_rank == -1` hole (a current-generation model absent
from `MODEL_LISTS` ranked below everything); the table has none. When the chain
names nothing on offer, `substitute_model` takes the same-family candidate
*nearest the pick's own cost position* (`_cost_position`, `:685`), biasing
cheaper on a tie — the earlier "most capable same-family" fallback inverted cost
outright, escalating every SIMPLE pi turn to opus because haiku is barred on pi
(`46a50556`). Ids compare with dots read as dashes (`_bare_id`, `:660`), so a
picker's `gpt-5.6-sol` matches the router's `gpt-5-6-sol` instead of missing
every chain and collapsing onto the priciest row, and the offered menu carries
one row per model rather than two. With live pre-session catalogs (§1.3) exact
matches are the common case and substitution is the exception, which is what the
matrix's "no fallback arrows" bar requires.

**Harness bars were unenforced on the turn path.** `_redirect_incompatible_pick`
(`:826`) stays in the seam as post-verdict harness correction:
`_HARNESS_EXCLUDED_MODELS` (`:639`) pairs are *not* pruned from the offered menu
when the harness is itself in play (the router needs its full menu or it 400s),
so an incompatible pick is moved to a harness that can run it instead. Two
corrections landed in `46a50556`: the redirect now takes the *offered* harness
set and declines rather than handing back a harness nobody offered — so a child
restricted to its parent's family can no longer escape onto `codex` or
`claude-sdk` — and a turn, which cannot change harness at all, prunes the models
its own gateway bars before offering them and swaps the *model* via
`substitute_model` when an injected arm comes back barred. `harness_bars_model`
(`:812`) is the shared predicate. `designs/LIVE_MODEL_STATE.md` documents why
each `pi` exclusion exists.

### 1.2 RoutingSettings on RuntimeCaps

`RoutingSettings` (`smart_routing.py:581`) is the frozen deployment record, and
it is down to three fields: `router_name`, `selection_model` (passed through as
`route_selector.config.model` so a deployment can pin an extraction model it has
query access to) and `model_prefixes`. `scenario_menus` went with the flattened
menu tables (§1.1), and `subagent_fail_mode` / `subagent_cache_ttl_s` went with
the knob and the cache they configured (§1.5) — all three in `36a17c65` /
`6112e6cb`. Everything reads the record through one accessor,
`routing_settings(caps)` (`:1057`), which returns all-defaults when caps carry
none — the de-scarring pass collapsed several ad-hoc re-parses into it, and fixed
routing settings being dropped from Docker's `RuntimeCaps` construction entirely
(`d181cbd5`).

The router client itself is chosen at *build* time — `cli.py` constructs exactly
one client into `RuntimeCaps.routing_client` — so there is no runtime fallback
chain. A router failure returns `None` with `last_error` set, surfaced through
`routing_last_error` (`:1077`), and callers proceed unrouted with the reason
attached (plan §2). This is what made the task_v1 rollback incident a logged
degradation rather than an outage. `last_error` is not part of the
`RoutingClient` Protocol (`:190`) — the accessor was always `getattr`-defensive,
so declaring it only implied a contract clients did not have (`36a17c65`).

Two logging-posture corrections belong here. The external router request body
carries up to 4000 characters of the user's prompt, so the request log drops to
DEBUG with the prompt replaced by its length (INFO keeps the router name and the
route options), and the judge client's raw response drops to DEBUG too
(`36a17c65`). The router's *rationale* paraphrases the prompt, so both entry
points log it at DEBUG and keep model/harness at INFO (`46a50556`).

### 1.3 Catalogs and spelling determinism

Three catalog sources, in order of preference:

- **Live per-session:** `fetch_runner_models` (`:219`) hits the runner's
  `/v1/sessions/{id}/models`; `catalog_models_for_harness` (`:122`) extracts the
  harness's slice.
- **Pre-session:** `_pre_session_model_catalog`
  (`server/routes/_sessions/orchestration.py:5620`) fans out to the host's
  pre-launch model options for each candidate harness. A create has no session,
  so the live catalog is out of reach; the host holds the CLIs and already
  resolves their picker options. Introduced with `158042a3` because create-time
  Smart Routing had been routing over the static tables, which is how a codex
  session got offered models it could not run. One helper now owns the host
  model-options round trip for both callers, and both readers accept picker rows
  spelled `model` *or* `id` (`3b00d101`).
- **Static:** `infer_models` (`:87`) as last resort, for harnesses the host
  cannot answer for.

Whatever the source, the candidate set is family-filtered by `models_in_family`
(`:105`), and family compatibility for codex is one shared authority:
`is_codex_compatible_model` (`model_override.py:126`) matches per id segment with
an optional trailing generation number, so `system.ai.glm-5-2` and
`kimi-k2-instruct` pass while a lookalike endpoint name (`glmqlfit-eval`) does
not. Before that, three independent gates — `model_catalog`, `model_override`,
candidate filtering — each rejected non-GPT ids on codex harnesses, which is why
GLM had been recorded as an external distribution gap: the codex catalog carried
`databricks-glm-5-2` and *we* stripped it (`158042a3`, §12 delta 9).

Spelling determinism was the other latent defect. The workspace lists the same
endpoint twice — `system.ai.claude-opus-5` and `databricks-claude-opus-5` — and
`databricks_model_discovery.py` answered with whichever listing happened to
succeed, so a routed turn could end up holding a spelling the pane would refuse.
Discovery now unions both listings, collapses duplicates onto the `databricks-`
spelling, and sorts versions on the bare id so a spelling can never outrank a
version (`972dea9d`). Separately, discovery returns the *full* servable Claude
catalog rather than newest-per-family: the workspace kept shipping newer
generations (`claude-opus-5`) while task_v1's arms are frozen at
`claude-opus-4-8` / `claude-sonnet-5`, and newest-per-family alias pins drifted
with the workspace (`af42b36c`, §12 delta 3). `discover_databricks_claude_models`
survives only as a deprecation shim over the catalog lookup, removed in v0.10.0;
its Unity Catalog short-circuit stays deleted, because UC only ever spells ids
`system.ai.` and skipping the gateway listing would make the catalog spelling
depend on which listing happened to answer (`3b00d101`).

### 1.4 Decision records and chip rendering

Every routing decision is a transcript item. `RoutingDecisionData` gained
`harness`, `scope` (`session` | `turn` | `child_session` | `native_subagent`),
`decision_id`, `raw_model` and `attempted_override`, all defaulted for legacy
rows (plan §5.2). `_emit_server_routing_decision`
(`server/routes/_sessions/helpers.py:5451`) writes it — after the decision
validates, not before a parse failure that produces no chip (`3b00d101`) — and
`_stamp_routing_decision_label` (`orchestration.py:4089`) records the decision id
on the session so a persisted `model_override` can be joined back to the decision
that produced it (`ROUTING_DECISION_LABEL_KEY`, `subagent_routing.py:87`). The
`routed_model` field on a child-session row is gated on that label, so a
user-pinned model no longer reports as routed with a null decision id
(`3b00d101`).

Only one component renders a decision: `RoutingDecisionCard` in
`web/src/components/blocks/StatusBlocks.tsx`, which is what `BubbleView` mounts.
A second `RoutingDecisionChip` existed unused for a while and was deleted, its
coverage moved onto the card (`2245f57d`). "Chip" below means the card in its
paired-below-the-message position.

Three rules matter for the UI, and all three exist because native sessions are
different:

- **`applied` must be honest.** See §2.4 — a decision that claims a model the
  process never ran is worse than a visible `applied=false`.
- **The chip renders below the user message it routed.** Native terminal
  sessions persist the decision *before* the message, so order-faithful
  rendering put the chip at the top of the chat. `deferredRoutingChips`
  (`web/src/lib/renderItems.ts:640`) pairs a session/turn-scoped chip with the
  adjacent user message and defers it below; already-correct orders are
  untouched, subagent chips never move, and streaming rebuilds the pair
  atomically in both arrival orders (`8fa280ea`). On claude only, the injected
  `/model` echo persists as a `slash_command` item *between* the decision and the
  message and broke adjacency — codex pushes over the app-server and emits no
  such item — so `isChipPairingSkippable` (`:383`, `:390`) skips `slash_command`
  blocks in both directions (`25b75c62`).
- **The incremental cache must count the region it just rendered.** The same
  `/model` echo renders its own bubble *inside* the chip↔message region, but the
  cache hardcoded two bubbles per region, so it dropped one too few and
  re-emitted the echo bubble on every later frame of any turn past the first
  (duplicate React keys included, until a full rebuild). The region now records
  `regionBubbleStart` (`:448`) and reports
  `lastBubbleCount = bubbles.length - regionBubbleStart` (`:484`), with a
  frame-by-frame test at a non-zero block offset — where the cache actually
  reuses; every earlier test started at block 0, where reuse bails out
  (`2245f57d`). The fix also split "renders nothing" from "may sit between chip
  and message", which `isChipPairingSkippable` had conflated.

The chips earn their keep: raw→applied divergence is how two real apply-layer
bugs were caught.

### 1.5 The route-subagent loopback and hook machinery

Native in-harness spawns never reach the server, so routing them needs a
runner-local endpoint the harness's own hook subprocess can call.
`omnigent/runner/subagent_routing.py` serves it:

- `start_subagent_router` (`:797`) binds an HTTP server on `127.0.0.1:0` and
  writes `subagent_router.json` (`{url, token, pid, session_id, updated_at}`)
  into the session's bridge dir — the same advertisement pattern as
  `tool_relay.json`. `SubagentRouter.close` (`:777`) removes it.
  `ensure_session_router` / `ensure_session_router_quietly` (`:997`, `:1040`)
  install it whenever a server client exists, *not* only for sessions that
  started routed, so a mid-session toggle-on has something to talk to (§5).
- `resolve_subagent_route` (`:475`) is the policy: it builds the candidate set
  with `candidate_models` (`:396`), calls the router, and returns a
  `SubagentRouteDecision` (`:235`) of `allow` / `rewrite` / `redirect` / `deny`
  with `model`, `harness`, `raw_model`, `rationale`, `decision_id` (plan §5.1).
  Unoffered picks are denied outright — "didn't spawn" beats "wrong model" — and
  that is the *only* remaining `deny`. The enablement gate is read **per call**
  one hop out, by the server relay route
  (`server/routes/sessions/routes_hooks.py:1246`) and the child-session path
  (`orchestration.py:680`), both through `subagent_routing_enabled` (`:156`),
  which layers the per-session override over the own/parent cost-control state.
- Family rules live here too: `harness_family` (`:337`), `model_in_family`
  (`:377`), and `auto_harness_session` (`:354`), which is what allows a
  cross-family pick *only* under the Smart Routing harness (§4.7).

Two pieces of this layer were built and then deliberately deleted (`6112e6cb`):

- **The configurable strict mode.** `subagent_fail_mode` (`open` / `closed`) was
  meant to make an unrouted spawn fatal, on the argument that it silently voids
  the determinism guarantee. But every failure path — no client, no candidates,
  router exception, empty verdict, transport error, hook timeout — already fell
  through to allow, so `closed` could not deliver what it promised. The gate is
  documented as **advisory** in the module docstring instead, `_unavailable_decision`
  (`:455`) is the single allow-and-say-why path, and the knob, its plumbing and
  the deny-on-failure branch are gone.
- **The per-`(session, task)` decision cache.** It saved a task_v1 extraction
  round-trip on identical spawns, but a cache hit re-emitted a `decision_id` that
  the contract documents as an *identity* — duplicating transcript rows and
  telemetry for one decision. Correctness won.

**Hardening.** The advertisement carries a bearer token, so `write_advertisement`
(`:702`) writes it through `os.open(..., 0o600)` into a temp file and
`os.replace`s it into place — never world-readable, not even for the instant
between a `write_text` and a follow-up `chmod`. The SDK harnesses have no bridge
dir of their own, so `router_dir_for_session` (`:1120`) gets them a private one
through the shared bridge-dir ancestor check (`ensure_secure_dir`,
`claude_native_bridge.py:740`) rather than `mkdir(mode=0o700, parents=True)`,
which applies the mode to the leaf only and trusts pre-existing ancestors on the
same `/tmp/omnigent-<uid>` path the bridge hardening was written to defend. On the
hook side, an advertisement is rejected unless it is plain http on `127.0.0.1` or
`::1` (`_is_loopback_url`) and its advertising pid is still alive
(`_advertiser_alive`). Token comparison is constant-time, and a 401/404 drains the
request body and closes the connection so keep-alive cannot mis-frame the next
request (`6112e6cb`).

**Lifecycle.** The router used to leak on two of three launch paths — only
claude-native tore it down — costing a `ThreadingHTTPServer`, a daemon thread, a
loopback socket, ledger entries and a live token file per session. Teardown is
now unconditional and idempotent: `shutdown_session_router` (`:1086`) is called
from both codex-native forwarder exits and the claude-native `finally`
(`runner/native/orchestration.py:4034`, `:4081`, `:6127`, all via
`_shutdown_session_router_async` (`:452`) because the close joins the serving
thread) and from the runner's session-delete path for SDK harnesses
(`runner/app.py:3484`). `close()` only unlinks advertisements still naming its own
url — sessions that fork/clear/resume keep the same bridge dir, so a newer router
may own the file — and every advertised dir is tracked and pruned
(`c46ef54d`, `6112e6cb`). Router env vars are scoped to the launching harness
(`session_router_env`, `:1142`), so a codex executor beneath a claude session no
longer inherits the parent's session id (`6112e6cb`, `de2acfdb`).

**Timeout budget.** Four hops wait on each other, so each is strictly larger than
the hop it waits on — otherwise an inner fail-open branch can never run. Harness
hook 40s > hook script `HOOK_REQUEST_TIMEOUT_S` 30s > runner relay
`RELAY_TIMEOUT_S` 20s > server hop `SERVER_HOP_TIMEOUT_S` 15s, documented in one
place (the module docstring) and aligned on the codex executor's outer timeout,
which had been a dead 120s (`6112e6cb`, `de2acfdb`). The relay ledger
(`_RELAYED_CAP`) and the agent-authored `task_name` (`_TASK_NAME_CAP`) are both
capped, since a long-running session can spawn without limit.

The hook scripts are one shared module, `omnigent/inner/hook_scripts/subagent_router.py`,
with thin per-harness entry points (`claude_router_hook.py`,
`codex_router_hook.py`). It is stdlib-only so it can be imported by a subprocess
on the spawn path. It discovers the advertisement (`discover_router_dir`,
`read_router_endpoint`), reads the parent model and the terminal's model
vocabulary out of `bridge.json` (`resolve_parent_model`,
`resolve_model_vocabulary_env`), builds the request (`build_route_request`),
calls the endpoint (`request_decision`) and renders the harness's hook output
(`decision_to_hook_output`, `route_pre_tool_use`). `run_route_subagent_main`
(`:620`) always exits `0`: routing must never be the reason a spawn fails. Fork
spawns are exempt in v1 (`FORK_SUBAGENT_TYPES`, `_FORK_SUFFIXES`).

De-scarring collapsed the per-harness duplicates into this one module and fixed
ten latent defects with regression tests in the process — including the hook
argparse exit-0 contract, codex fork detection, cross-harness label agreement and
family-filtered candidates (`d181cbd5`).

### 1.6 The enforcement canary

Hooks that silently do not run are the worst failure mode available: the UI shows
routing on, the spawns are unrouted, and nothing complains. The canary is the
detector — a `SessionStart` hook writing a file into the bridge dir, plus a
watcher that posts the session-scoped warning `subagent_routing_unenforced`
(`runtime/session_warnings.py:31`) when the file is absent. Its arming logic had
to be inverted before it worked; see §3.7, where it caught both codex apply-layer
bugs. The warning is retractable as well as postable — see §5.3.

### 1.7 Telemetry

Routing telemetry is OSS analytics events, not OTel spans:
`RoutingDecisionEvent` and `RoutingSettingChangedEvent`
(`omnigent/telemetry/events.py:90`, `:142`), emitted through
`record_routing_decision` / `record_routing_setting_changed`
(`omnigent/telemetry/routing.py:41`, `:91`). The original shape — a span-event
helper in `runtime/telemetry.py` — was rejected in review because it read as
debug-only and manufactured orphan spans when nothing was recording. The
replacement is allowlist-posture: model ids reduce to `model_family` /
`model_tier` labels, and rationales, prompts and task names never leave the
transcript. Routing enablement is state on `SessionCreatedEvent` rather than an
event fired on router installation, because installation happens in the runner
process where the analytics client never initializes; the parent-transcript
mirror no longer double-counts; the OTel helper and its constants are deleted
(`c7f78f26`).

There is no browser-side routing telemetry. A `web/src/lib/routingTelemetry.ts`
recording `ROUTING_DISABLED_MID_SESSION` and `ROUTING_FORK_FROM_ROUTED_SESSION`
shipped and was deleted with both call sites: no user-visible value, and the fork
predicate was wrong. `ForkSessionDialog`'s session fetch went with it — it existed
only to feed that event (`2245f57d`).

---

## 2. Claude Code CUJ, end to end

### 2.1 UI entry point

Smart Routing is a **Model** choice in *Configure Claude Code*, gated on the
server's `smart_routing_enabled` capability and offered only for `claude-code`
and `codex` (`web/src/shell/NewChatDialog.tsx`, `smartRoutingEligible` at
`:2281`). Picking it freezes the Effort row to an em-dash — the router picks
effort per task, so showing a live value would lie — and leaves Permissions
alone. Rationale (plan §10 decision 1): routing is a property of *which model
runs*, so it belongs in the Model dropdown rather than as a fourth control users
must find. The gear tooltip mirrors the modal (`configSummary`, `:2312`, routing
row at `:2341`) so the active setting is readable without opening it, and gates on
eligibility so a stale `"on"` never renders a misleading routing row.

There is deliberately **no** in-session "Model = Smart Routing" toggle: main-agent
routing is a session-start concept and an in-session switch would imply something
that cannot take effect (plan §10 decision 4). The in-session control is
*Subagent routing* instead (§5.2).

### 2.2 Session create payload

The create carries `cost_control_mode_override: "on"` and **no** model or effort
pin. That is the whole handshake: routing runs only when nothing is pinned, so
the absence of a pin is what arms it. Evidence for this layer is the
`session_overrides` column on the created row (`CUJ_STATUS.md` §1).

### 2.3 The turn gate and the routing call

`_forward_event_to_runner` (`server/routes/_sessions/orchestration.py:3641`)
computes `effective_runner_override` (`:3794` — per-event override, else the
persisted column, `is not None` and never `or`, per the no-invented-defaults rule)
and then the `_should_route` gate (`:3890`): routing enabled, event is a
`message`, the auto-harness block did not already route this turn, and no model is
pinned. In practice that fires on the **session's first message only** — the
routed turn persists its pick as `model_override`, which is itself a pin, so turn
2 onward reuses the routed model without consulting the router.

A set of per-branch INFO logs naming *why* a route was declined shipped with
`c2f79f1c` ("auto-harness already routed this turn", "model already pinned (…)",
"event type … is not a message") and were removed in `3b00d101`: each one
restated the condition of the branch it sat in, so they carried no information the
gate expression did not already state. The declined-route diagnostics that
remain live one level down, in `route_turn` itself, where the reason is *not*
inferable from the caller: no routing client configured, no candidate models for
the harness, and the harness bars every candidate.

`route_turn` (`smart_routing.py:1525`) scores the raw user text — 4000-char cap,
no wrapper or summary, because `task.prompt` is the entire routing signal
(plan §1.1) — against the `cc` scenario menu, with candidates filtered to the
Claude family.

### 2.4 Decision persistence and the chip

"Can this pane actually apply the pick?" is answered *before* anything is
persisted, by `_routed_turn_model_spelling` (`orchestration.py:3551`). A
mid-session switch on a Claude pane is typed as `/model`, which accepts only that
session's own picker vocabulary; a routed id outside it is skipped by the executor
(fail open, the turn runs on the current model). So the server runs the *same*
translation the executor will — `model_vocabulary_env` over the session's cached
picker rows, then `claude_model_command_arg` — and returns `None` when the pane
has no spelling for the pick. Unknown vocabulary (no picker rows cached yet)
returns the routed id unchanged: the launch env is the only authority and guessing
either way is its own inaccuracy.

`None` now gates the *whole* downstream sequence, not just the chip. The first
version of this check (`_mark_unapplied_native_turn_decision`, `af42b36c`) only
corrected the verdict to `applied=false`, while the pin was written anyway — and
because any `model_override` blocks routing (§5.1), one unapplicable pick
permanently disabled routing for that session and misattributed its usage to a
model it never ran. `3b00d101` collapsed the marker into this pre-persist check:
no spelling means no `model_override`, no in-band switch, and
`_unapplied_routed_verdict` (`:3600`) appends the reason to the rationale and
clears `applied`. Honest `applied=false` beats a silent lie, and it is what makes
the matrix's no-arrows bar meaningful.

The chip is emitted *after* the runner forward and `input.consumed` so the live
SSE stream delivers the user bubble first, and rendered below the message per
§1.4 — including the `slash_command` skip that only claude needs.

### 2.5 The apply layer

This is where nearly all the work was. Four separate problems, in launch order.

**Launch env pins.** claude-native launches its terminal *before* any turn
decision exists, and `/model` can only reach ids the launch env spells. So when
`launch_metadata.routing_enabled`, `runner/native/orchestration.py:5895-5898` pins
the family aliases at the router's frozen arms via
`claude_config_with_routed_arms_pinned(claude_config, task_v1_claude_arms())` —
the arm list is read from `_TASK_V1_CLAUDE_ARMS` through that one accessor
(`smart_routing.py:568`) rather than duplicated. Without this, `/model
opus` landed on whatever the workspace's newest opus was (`claude-opus-5`) while
the chip claimed the routed arm (`claude-opus-4-8`) — the workspace moving ahead
of the frozen router (`972dea9d`, §12 delta 3).

**The custom picker slot.** Claude Code has exactly one extra picker slot that
takes an *exact* id. `claude_config_with_launch_model_pinned`
(`claude_native.py:435`) parks the launch model there when no alias spells it —
the case a Smart Routing create hits, since the harness CUJ resolves an exact
model before the terminal exists (§4). It also gives the user a picker row to
return to. Both pin sets are persisted into the bridge config as `model_env`
(`claude_native_bridge.py:889-895`, keys `MODEL_VOCABULARY_ENV_VARS`, read back by
`read_model_env` at `:1067`) because the executor and the server do not share the
terminal's environment and both need to know its vocabulary.

**The vocabulary itself.** `omnigent/claude_model_vocabulary.py` is the shared
authority, stdlib-only so a hook subprocess can import it on the spawn path. Its
premise, learned the hard way: Claude's model vocabulary is *closed*. The
`Agent`/`Task` tool's `model` parameter is an alias enum (`sonnet`, `opus`,
`haiku`, `fable`), so a catalog id fails schema validation and the spawn dies
before it starts; `/model` accepts an alias, the byte-exact
`ANTHROPIC_CUSTOM_MODEL_OPTION` value, or an arbitrary id only if a live
one-token endpoint probe succeeds mid-turn. So a servable catalog id is *not* a
spelling the harness accepts, which the plan's single-step `resolve_selection`
had assumed. The module inverts the `ANTHROPIC_DEFAULT_*_MODEL` pins
(`alias_pins`), reconstructs a vocabulary from picker rows (`model_vocabulary_env`),
and translates: `claude_model_alias` (`:132`) for the Agent enum,
`claude_model_command_arg` (`:176`) for `/model` (which additionally honours the
custom slot's exact id). `normalized_model_id` (`:68`) is the same file's
comparison spelling, reused by the codex spawn audit (§3.7) so a prefix or case
difference is never read as a different model. Translation requires an **exact**
pin match — a family segment
alone is not enough, because with `opus` pinned to `claude-opus-5` the alias
would run a model nobody routed to while the record claimed the routed one. Both
surfaces fail open: `None` means "leave the model alone" (`539b00ae`,
`af42b36c`).

**The routed model never reached the executor.** The plan listed native `/model`
injection as existing capability and treated applying a routed model as solved
plumbing. In reality the runner's `_run_turn_bg` (`omnigent/runner/app.py`)
rebuilt the harness request field by field and never copied `model_override` off
the incoming message, so the executor's injection branch never ran: routed
sessions kept their launch model, while `/effort` (a separate session change)
worked, which is exactly why nobody noticed. The field is now forwarded
explicitly, with INFO logs at every hop — server forward, runner intake, turn
dispatch, executor type-or-skip-with-reason — so the chain cannot go silent
again (`82cac6fa`). From the runner it reaches the executor as
`ExecutorConfig.model` (`runtime/harnesses/_executor_adapter.py:285`).

**The switch itself.** `ClaudeNativeExecutor.run_turn`
(`inner/claude_native_executor.py:107`, switch-and-inject at `:155-195`) applies
the switch and injects the message as one step under `_inject_lock`:
`inject_slash_command("/model <arg>", auto_confirm=True)` runs to completion, then
`inject_user_message`. Folding them under one lock removed a second writer on the
same tmux pane — routing used to switch the model with a separate server-issued
`model_change` event that raced the inject, so the message's keystrokes could land
mid-switch and be lost (the "routing drops the first message" bug).
`_model_command_arg` (`:196`) gates the typing on two conditions:
`_should_switch_model` (`:253` — the pane is not already on it, baseline seeded
from the spawn `launch_model` so turn 1 compares against what Claude actually
booted with) and a successful vocabulary translation. Every skip logs its reason.

### 2.6 Live-state visibility

Because the switch goes through `/model`, the harness's own UI reflects it: the
pane echoes the command and prints the new model banner, which is exactly the
process-truth handle the matrix uses (`tmux capture-pane`, plan §11.2). Claude
Code persists that pick as the machine's default model, so a routed session
leaves the user's next manual `claude` launch on the routed arm — accepted for
MVP because the harness config modal's Model picker already behaves that way
(plan §10 decision 8).

### 2.7 Subagent routing

`build_hook_settings` (`claude_native_bridge.py:1188`) registers
`claude_router_hook` as a `PreToolUse` hook on the `Task|Agent` matcher (`:1425`);
settings-level hooks recurse into nested subagents. The hook rewrites
`tool_input.model` via `hookSpecificOutput.updatedInput` with
`permissionDecision: "allow"`, or denies. Because the Agent tool's `model` is a
closed enum, the hook translates through `claude_model_alias` with the vocabulary
read out of `bridge.json` (`claude_model_translator`,
`hook_scripts/subagent_router.py:403`) — this is what turned a 7 ms schema failure into a
spawn that ran to completion on the routed arm (`CUJ_STATUS.md` §4). Candidates
are family-filtered, so a `cc` session can never spawn a Codex arm. Mid-session
toggling is §5.2.

### 2.8 Warnings and telemetry

Claude's hooks are settings-level and cannot be untrusted, so there is no canary
path here; the visible signals are the decision chips, the per-subagent model in
the sub-agents panel, and the `RoutingDecisionEvent` per decision (§1.7).

---

## 3. Codex CUJ, end to end

### 3.1 UI entry point

Identical entry: Smart Routing in the Model row of *Configure Codex*, same
eligibility gate. Codex folds routing into its Model row rather than exposing a
separate toggle, so the gear tooltip reports it the same way Claude's does
(`NewChatDialog.tsx:2341`).

### 3.2–3.4 Create, gate, decision

Same create payload (`cost_control_mode_override: "on"`, no pin), same
`_should_route` gate, same `route_turn` call — over the `codex`
scenario menu (all three Codex arms), with candidates filtered by
`is_codex_compatible_model`, which is what makes `databricks-glm-5-2` survive to
an applied pick (§1.3). Chip pairing needs no `slash_command` skip here: codex
pushes the model over the app-server and emits no transcript item for it.

### 3.5 The apply layer

Applying a model to codex has **three writers**, which neither the plan's §2 nor
§3 anticipated, and they fought each other:

1. `thread/settings/update` switches the running thread (and only that — it does
   not write `config.toml`).
2. The per-session `config.toml`'s top-level `model` key is what an in-TUI
   `/model` writes, and what omnigent's own readers use — the forwarder's mirror
   and the cost-gate hook.
3. The launch pin `_pin_codex_config_model`
   (`codex_native_app_server.py:203`) seeds that key, and the TUI is launched
   with the same value as `-c model="…"`.

The observed symptom was that the routed model survived exactly one turn. Turn N
routed, the executor pushed the thread, the rollout genuinely ran the routed
model — then the forwarder's `turn/started` handler re-read the **stale**
`config.toml` and posted `external_model_change(launch default)`, the server
persisted it as `model_override`, and turn N+1 skipped routing ("model already
pinned") and pushed the default back onto the thread. Every surface settled back
on the launch model. `designs/LIVE_MODEL_STATE.md` has the full trace and the
protocol probes behind it.

What shipped (`0fcc313f`, `51801530`):

- **First-turn push under the inject lock.** The forwarded message carries
  `model_override` in band; `CodexNativeExecutor.run_turn` sends
  `thread/settings/update` before the bare `turn/start`, under `_inject_lock` —
  the same switch-then-inject discipline as claude-native. This also closes the
  launch race by construction: a terminal auto-created at session bind precedes
  the first message, so no re-read timing could help, but every turn re-applies
  `ExecutorConfig.model` and the thread converges on the routed model at turn 1.
- **A `config.toml` mirror on a successful switch** (`write_codex_config_model`,
  `codex_native_bridge.py:315`), writing the same key the TUI's `/model` writes, so
  the cost gate and the mirror agree instead of diverging.
- **Forwarder precedence** (`codex_native_forwarder.py:2737`,
  `_refresh_model_from_config`): the state tracks `settings_model` (last live
  `thread/settings/updated` — the running thread's truth) and `last_config_model`.
  A config value that *changed* since the previous read wins (a genuine in-TUI
  `/model`, or our own mirror write); an unchanged one defers to the pushed
  model. This keeps the routed model even when the mirror write fails, while
  still honouring a user's in-TUI switch. `_sync_model_change` (`:2774`) posts
  `external_model_change` only on a real difference, and the server dedupes
  against `conv.model_override`, so no echo loop.
- **A `session.model` SSE at routing persist time** (`_publish_routed_model`,
  `orchestration.py:3617`) so the web dropdown tracks live state instead of
  waiting for a reload. It carries the spelling the session's picker uses — a tier
  alias, not a catalog id — because that is what the dropdown matches against, and
  the native path publishes picker vocabulary too (`3b00d101`).

### 3.6 Live-state visibility

Probed live on codex-cli 0.145.0: `thread/settings/update` requires the
`experimentalApi` capability (already sent), emits `thread/settings/updated`, and
that notification is broadcast to other clients that resumed the thread — so the
`--remote` TUI receives it and its **bottom status bar updates immediately**. The
startup banner box is static and the `/model` picker does not highlight models
outside its own catalog; both are upstream TUI behaviour, recorded rather than
worked around. With the reversion loop fixed the thread genuinely stays on the
routed model, so `/status`, the status bar and a resumed TUI all agree.

### 3.7 Subagent routing

Codex needs the most machinery of the three, and every piece of it was forced by
a live failure.

**Hook generation and merge.** `codex_router_hooks_settings`
(`inner/codex_executor.py:917`) builds the Omnigent half of a `hooks.json`:
a `PreToolUse` gate on the spawn tool, a `SessionStart` canary, and a
`SubagentStart` audit writer. The spawn matcher is the regex `.*spawn_agent`
(`_CODEX_SPAWN_AGENT_MATCHER`, `:865`) because codex flattens the tool name
(`collaborationspawn_agent` on 0.145.x). `write_codex_router_hooks_file` (`:1067`)
merges it with the user's hooks for the SDK executor path; the app-server path
merges policy + routing + user hooks in `_write_codex_policy_hooks_file`
(`codex_native_app_server.py:1007`).

**One writer, one file, and probe the version first.** Arming subagent routing on
codex < 0.129 used to *delete the user's hooks*: `_populate_codex_home_config`
dropped the symlink to `~/.codex/hooks.json` because the generated file was going
to own that name, and only afterwards did the version gate decide not to write one
— leaving the private `CODEX_HOME` with no `hooks.json` at all. The version is now
probed before the home is populated (`app_server.py:629-641`), so an unsupported
codex keeps the symlink. The root cause was two divergent `hooks.json` writers,
whichever ran last erasing the other's contribution; they collapse onto one shared
`write_codex_hooks_file` taking a *list* of payloads — policy, routing and the
user's own — merged into a single atomic write (`c46ef54d`).

**`--dangerously-bypass-hook-trust` is a no-op for app-server-dispatched hooks.**
The plan recorded the bypass flag as existing groundwork and assumed the trust
gate was handled. A live probe matrix showed the generated routing hooks stayed
untrusted and were *silently skipped* while the policy hooks worked — only the
policy module's hashes had ever been persisted. Both app-server launch paths now
run a persisted trust handshake for the router hook module: `hooks/list` →
`config/batchWrite` of `hooks.state.<key>.trusted_hash = currentHash`
(`_persist_hook_trust`, `:1156`) → re-list to verify
(`trust_codex_router_hooks`, `:1193`; policy equivalent
`trust_native_policy_hooks`, `:1256`), both driven immediately after the
app-server connects (`:773-780`) and filtered by hook module so the trust step
never touches hooks the user's own file contributed. It is best-effort and
isolated, so a routing-trust failure can never disable the policy gate
(`e32c4925`). The flag itself survives only where it actually works — the
interactive TUI launch (`runner/native/orchestration.py:3809`) — and nothing on
the app-server path relies on it. A codex version we cannot parse is treated as
*supported* on both paths, so a flaky probe can never wedge a terminal on a prompt
no subagent can answer (`c46ef54d`).

**`python -I`.** The plan's hook scripts were "pure functions around the endpoint
call", with no thought given to how they get imported. Codex runs hook commands
with the *session workspace* as cwd, and `python -m` puts cwd first on
`sys.path` — so a workspace containing an `omnigent/` directory (this repo being
the single most likely workspace) shadowed the installed package and every
generated hook died on import, silently: routing gate, canary, spawn audit and
the policy hook alike. `_codex_router_hook_command` (`:874`) now runs
`python -I -m …`, matching the bridge MCP command's posture, with a subprocess
regression test that runs the real canary from a workspace containing a decoy
package (`518376ba`).

**The canary was a circular detector.** As built, the watcher gated on the relay
ledger that the broken hooks would have populated — so a *total* hook failure
looked like silence. It now arms on the router advertisement
(`subagent_routing_armed`, `codex_native_forwarder.py:5748`) and anchors on the
first turn (codex fires `sessionStart` at first turn, not thread start), and
`_watch_subagent_routing_enforcement` (`:5809`) posts
`subagent_routing_unenforced` within a tick when the canary file is absent
(`e32c4925`). Its message was widened to "untrusted, or the hook command failed"
once cwd shadowing proved the second mode existed (`518376ba`), and the watcher
task is cancelled on teardown so a session that never takes a turn cannot leak it
(`c46ef54d`). This watcher is what caught both codex bugs;
`reconcile_spawn_audit` (`codex_executor.py:1194`) additionally compares the
`SubagentStart` audit's actual `model` against the models we routed to — through
`normalized_model_id`, because codex reports its own spelling and a prefix or case
difference is not a different model (`c46ef54d`).

**Encrypted spawn payloads and the no-signal path.** Codex encrypts the spawn
`message` in hook payloads, so routing must live on `task_name` plus metadata —
the plan knew that but assumed a name is always present. Live spawns frequently
carry neither task nor agent name, and feeding the router an empty task produced a
400 that surfaced on the chip as a router outage. The first answer was ucode PR 251
parity: route unnamed spawns on the fixed placeholder task `"Codex subagent task"`
(short and code-free, so deterministically the cheap arm), disclose exactly what
was scored, share one router call across identical no-signal spawns, and announce
the rewrite in the TUI via a `systemMessage` (`with_system_message`,
`hook_scripts/codex_router_hook.py:96`, still the codex hook's post-processor)
(`e034d86a`). That was replaced in two steps. `a95105c9` short-circuited
signal-free spawns to allow-with-parent-model, since the `SubagentStart` audit
proves spawns inherit the routed thread model — keeping both the chip and the audit
reconciliation truthful. `6112e6cb` then deleted the placeholder task and its
disclosure marker outright: `_routing_task` (`subagent_routing.py:440`) returns
`None` when there is no signal, and `_decide` (`:557`) allows the spawn unchanged
on `req.parent_model` with the rationale "No routable signal (encrypted prompt, no
task name); subagent inherits the session model". Nothing is scored on a synthetic
prompt any more, so nothing has to be disclosed.

### 3.8 Warnings and telemetry

`subagent_routing_unenforced` is delivered on the session-status channel and
rendered as a session warning banner. Its visibility rule is §5.3. Decision
telemetry is the shared path (§1.7).

---

## 4. Smart Routing (auto) harness CUJ, end to end

### 4.1 UI entry point

Smart Routing is its own **unlabeled dropdown group above** the Harnesses list
(`NewChatDialog.tsx:1160-1175`), with the helper blurb dropped: it routes *over* the
harnesses rather than being one of them (`76749e03`). The label churned three
times — "Intelligent Routing" → "Auto Harness" → "Auto" → **Smart Routing** —
and the final rename swept every user-facing surface (harness chip, dropdown
item, Configure modal, the Claude Code / Codex Model option, the in-session
subagent row, decision chip and card headers, the `sys_advise_models` tool title,
the subagents-panel tooltip) while deliberately leaving API fields, storage keys,
sentinels and telemetry names unchanged (`e5c8a160`). Labels live in
`web/src/lib/agentLabels.ts` (`SMART_ROUTING_LABEL`, `AUTO_HARNESS_ID` = `"auto"`,
`AUTO_NATIVE_HARNESS_ID` = `"auto-native"`).

**Persistence and degrade.** The pick is remembered through the same
last-harness store as every other harness (`handleSelectSmartRoutingHarness`,
`:2987`): it is stored under the placeholder wrapper agent's id as
`AUTO_NATIVE_HARNESS_ID`. A restored sentinel that cannot be honoured — routing
disabled, or the native arm missing on this host — degrades to the default pick,
and clicking the placeholder wrapper's own row clears the remembered sentinel so
the explicit choice is what survives a reload (`ee26ff7c`). The landing's
"Smart Routing dropped" notice was itself wrong for a while: it always blamed host
readiness, fired for a `localStorage` pick that was never available this visit, and
stacked with the harness-readiness notice. The cause is derived and quoted now, the
announcement requires an availability loss *while the landing is open*, and the
readiness notice wins the slot (`2245f57d`, `9c81bbb8`).

**Configure Smart Routing is Permissions-only, locked to a disabled "Default".**
No Model or Effort rows — the router owns both — and the create payload carries
**no** permission override at all, so the picked harness inherits the machine's
own defaults, byte-identical to launching that harness natively. Stale stored
modes for the sentinel are no longer read (`320b6b59`). A cross-harness
permission mapping (Claude modes vs codex `approval_policy` × `sandbox` ×
profiles) was researched and deliberately deferred: the four-way mapping has
enough asymmetry that shipping it wrong would silently loosen sandboxing. The
disabled row keeps the slot visible for when it lands (plan §10 decision 3).

### 4.2 The create payload

Two fields do the work: `harness_override: "auto"` and
`smart_routing_message` — the user's first-message text
(`server/schemas.py:1380`, sent at `NewChatDialog.tsx:3236`). It carries the text
as *delivered* (mention preamble plus sanitization), not the raw box contents, so
the router scores what the harness will actually see (`2245f57d`). The create needs
a concrete `agent_id`, so the client binds the Claude native wrapper as a
*placeholder*; the picker suppresses that row's highlight while the sentinel is
active so it does not look like a Claude Code pick.

### 4.3 Create-time routing over native harnesses

A native session's harness cannot wait for the first message the way the
bundle-agent auto path does — the terminal launches as soon as the session row
exists. So `_resolve_native_smart_routing` (`orchestration.py:5649`) routes at
create: it authorizes the caller's `host_id` (`resolve_host_owner`, `:5684`), reads
the host, filters `AUTO_NATIVE_ROUTING_HARNESSES` (`smart_routing.py:1341`) to the
CLIs actually installed (`_installed_native_harnesses`, `:5537`), calls
`route_session_harness` over the `both` five-arm menu with candidates from
`_pre_session_model_catalog` (§1.3), and returns the chosen native **wrapper
agent name**. The caller rebinds `agent` to that wrapper
(`:5798-5820`), and from there the create is byte-identical to a normal native
create, terminal launch and all — nothing is launched twice. The routed model is
threaded into `validate_session_model_metadata` (`:5841`) as the session's
`model_override`, so it reaches the CLI as a `--model` argv element at launch,
which is a different (and more permissive) contract than `/model`: `--model`
takes any string verbatim. That is why the harness CUJ needs the custom picker
slot (§2.5) — the session boots on an exact id no alias spells.

The host authorization is not incidental ordering. As first written, this function
read the host's harness readiness and pushed `HostModelOptionsFrame`s over its live
connection roughly 150 lines *before* `_validate_session_workspace` authorized the
caller — leaking CLI and catalog presence on a foreign host, and landing frames in
another user's host connection. `resolve_host_owner` runs first now (`3b00d101`).

`harness_override` is stored as `None` rather than `"auto"` on this path: a native
wrapper rejects a harness override, and leaving the sentinel behind would make the
first message re-route an already-running terminal. Auto-ness is instead recorded
as a durable label, `omnigent.routing.auto_harness`
(`subagent_routing.py:93`) — the sentinel is consumed at first message, so
nothing else would survive to answer "was this session genuinely Smart Routing?"
(`0fb7ea95`).

Routing unavailable does not fail the create: it lands on the first installed CLI
with that CLI's own default model, and returns an `error` string the routing card
shows. `_resolve_native_smart_routing` returns `None` for the agent only when no
native CLI is installed at all, which is a hard 400.

### 4.4 The double-resolution fix

This CUJ is where §1.1's seam bug was fatal rather than cosmetic:
`route_session_harness` fed the client's already-local pick back through
`resolve_selection`, no arm matched, the harness came out `None`, and the create
silently degraded to the fallback harness — the "auto sessions lost their
harness" failure. `972dea9d` fixed it by resolving from `raw_model` and making
`resolve_selection` idempotent; `36a17c65` then removed the second resolution pass
entirely, so `route_session_harness` applies the client's `model` verbatim and only
derives a harness when the client named none. The 2026-07-30 matrix re-run closed
rows A1–A4 on the first fix, with panes showing Opus 4.8 and zero `harness=None`
warnings in the log.

### 4.5 Decision, chip and live state

One **session**-scope decision is persisted, carrying both the harness and the
model, and rendered as the decision card under the first user message per §1.4.
Beyond that the session behaves exactly like §2 or §3 depending on which arm won,
including the codex thread push or the claude `/model` path for any later change.

### 4.6 No re-routing after session start

Turn 2 must not produce a second session-scope decision. The harness pick is
*physical* — a session is a live `claude` or `codex` process with its own bridge,
config and pane — so "re-route turn 2" means killing and relaunching a process
mid-conversation. Recorded as plan §10 decisions 4 and 7; per-turn harness
routing waits on the router's unused `session_history` field.

### 4.7 Cross-harness subagents, only here

Spawns under a genuine Smart Routing session may pick either family. Everywhere
else they may not, and the naive version of this rule was a live bug:
`_force_auto_for_child` treated *any* routed parent as Smart Routing, so every
child of a plain codex or claude session got `harness_override: "auto"`, was
routed over a family-mixed catalog, and inherited the cross-family escape hatch —
found live as a codex parent with nine forced-auto children, some on
claude-opus. Now the auto treatment requires the parent to actually be in auto
mode (`auto_harness_session`, checked at `orchestration.py:5877`), child routing
passes the parent's family as a candidate filter (`allowed_family`,
`:3914-3926`), and `route_turn` drops out-of-family models from the self catalog
(`5a397d6f`). `46a50556` closed the last escape: the post-verdict harness redirect
(§1.1) used to hand back `codex` or `claude-sdk` whether or not they were on offer,
so a family-restricted child could still land outside its family. It now declines
unless the replacement was itself offered, and swaps the model instead.
The same family rule backs the hook path for in-harness spawns, so
native spawns and omnigent child sessions cannot disagree.

### 4.8 Warnings and telemetry

Same as §1.7. The routing card's `error` string is the only auto-specific
surface: it is how a degraded create ("Routing unavailable; using the default
native harness.") explains itself.

---

## 5. What all three CUJs share at run time

### 5.1 Session-start routing, then session-pinned

Routing runs **once per session**, on the session's first message, and the model
it picks persists for the session's life. There is no per-turn re-routing.
`_should_route` requires no effective override, and the routed turn itself writes
`model_override` — so the pin the router installs is what stops turn 2 from
routing again. For the Smart Routing harness the same pin is written at create
instead. A manual pick blocks routing by the identical rule: any `model_override`
is an effective override. The per-branch "not routed because X" INFO logs that
first made this legible — and let the codex reversion loop be recognised as
"model already pinned" rather than "routing broken" — were removed once the gate
expression said the same thing on its own (§2.3).

### 5.2 Subagent routing: inherit, override, per-call

`subagent_routing_override` on the session is `"on"` / `"off"` / `null`, with
`null` inheriting the session-start choice — a Smart Routing main agent routes
its subagents; a manually pinned one does not. The in-session gear row exposes it
for Claude Code, Codex (native and SDK) and Smart Routing sessions, toggleable at
any time and effective on the next spawn (`0fb7ea95`, web `1d030f22`, sticky
per-harness default `2a415cf4`).

**"Inherit" is its own option** in that row (`web/src/pages/ChatPage.tsx:5615`,
`:5778`). It used to collapse onto the effective `on`/`off`, which broke twice over:
Radix fires no `onValueChange` for the value already displayed, so re-picking the
inherited value silently persisted nothing, and the row labelled it "Default" for
sessions the spec routes by default (`2245f57d`).

The original design read the enforcement decision once, at launch, which locked a
routed session into enforcing subagent routing forever and made a mid-session
toggle impossible. Two changes make it work: the enablement gate is re-read **per
call** on the way in (`subagent_routing_enabled`, §1.5), and hooks install
whenever a server client exists rather than only for sessions that started
routed, so toggling *on* mid-session has an endpoint to reach. Verified live in
both directions on both harnesses (matrix rows B-tog / C-tog): off declines per
call with no decision persisted and the spawn proceeds; on routes the very next
spawn. Toggles emit `RoutingSettingChangedEvent`.

### 5.3 Warning hygiene

Once hooks install unconditionally, the canary warning fires on sessions with
routing *off* — a direct consequence of the previous change. The recorded
observation stays durable, but visibility is re-derived per session-snapshot
build using the same effective gate the relay applies (override, else own/parent
cost-control state) — `orchestration.py:677-689`. So a mid-session toggle-on
reveals the warning and toggle-off clears it, without re-posting anything
(`5444a1a4`).

Three follow-ons made the banner behave:

- **Warnings are clearable.** `session_warnings.clear(session_id, code=None)`
  (`runtime/session_warnings.py:93`) is called when a relayed spawn proves the hook
  did fire (`routes_hooks.py:1235`), when a publisher posts an empty list
  (`routes_events.py:742`), and on session delete (`:1688`). Codes are allowlisted
  and payloads reduced to known string fields, so the index cannot grow arbitrary
  shapes (`3b00d101`, empty-list clearing `c46ef54d`).
- **The banner can appear without a reload.** Warnings are recorded server-side
  while the session runs, with no event channel of their own, so the web client
  polls the open session's snapshot. That poll originally asked for
  `refresh_state=true` on *every* fetch, popping the runner's skills and
  model-options caches twice a minute per open session forever; only the cache-cold
  read refreshes now, and the poll stops after two consecutive 404s (`2245f57d`,
  `9c81bbb8`).
- **Unknown codes cannot break the header.** `SessionWarningBanner`'s copy table is
  a `Map` (`web/src/shell/SessionWarningBanner.tsx:26`), not a plain record: a wire
  code naming an `Object.prototype` member (`__proto__`, `toString`) passed the
  old filter and then threw while rendering (`9c81bbb8`).

### 5.4 Fail-open, with the reason attached

Every path fails open. A router outage leaves the turn unrouted with `last_error`
surfaced to the UI (proven by the task_v1 rollback incident, where 400s produced
unrouted-and-logged rather than broken turns). The spawn path was *meant* to be the
exception — a configurable `subagent_fail_mode` whose `closed` setting would refuse
the spawn, on the argument that an unrouted spawn silently voids the determinism
guarantee — but every failure branch already allowed, so the knob promised
enforcement it never delivered. It is deleted, and the gate is documented as
advisory in `subagent_routing`'s module docstring (`6112e6cb`, §1.5). The one
surviving `deny` is a router pick outside the offered menu, which is a *wrong*
answer rather than a missing one. In every case the decision record is the only
durable log of a routing decision — AIGW has no server-side decision logging yet —
which is why `applied` honesty (§2.4) and raw→applied divergence on the chip
(§1.4) are load-bearing rather than cosmetic.

---

## 6. Known-open items

- **Codex spawn naming rarely reaches hooks**, so most codex spawns carry no
  routable signal at all and are allowed through on the parent's model rather than
  routed (matrix row C-sub). The routing gate is real on those spawns, but it has
  nothing to score.
- **task_v1 prices well-written prompts at opus.** P-OPUS escalates because it is
  clear, contained and code-referencing, and under the `both` scenario the
  GLM-shaped case escalates too rather than delegating. The recipe does what it
  says; it is frozen, so this is task_v2 feedback for the AIGW team, not a
  client-side change.
- **Cross-harness permission mapping** stays deferred; Configure Smart Routing
  keeps its disabled Permissions row as the slot for it.
- **Fork spawns** are exempt from routing in v1, test-pinned only.
