> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Component: Agents / Subagents / Routing / Inbox

Scope: custom-agent storage + caching, sub-agent spawning + info propagation + depth,
intelligent routing, async/inbox, resume dispatch. Verified against the running code in
`/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs` + trace
`conv_fc47380ccbff481abf452a446ec4e40d` (the subagent-spawn / "BANANA" Polly run).

---

## 1. Role & boundaries

**Owns:**
- Sub-agent spawn surface (`sys_session_send` / `sys_session_create` and the read/observe tools
  `sys_session_list/get_history/get_info/close/share`) — schemas + in-process impls in
  `omnigent/tools/builtins/spawn.py`; **the actual minting** of a child Conversation on the
  **runner** in `omnigent/runner/tool_dispatch.py::_execute_subagent_tool`.
- Child↔parent result return via the per-session **inbox** (`asyncio.Queue` per session) +
  `async_work_complete`-style drain (`sys_read_inbox`); async tool dispatch (`sys_call_async`).
- Custom-agent storage tiers (ArtifactStore → `Agent` DB row → server `AgentCache`) and the
  separate runner-side bundle disk cache.
- Server-side intelligent model routing (`omnigent/server/smart_routing.py::route_turn`).
- CLI `omnigent resume` dispatch (`omnigent/resume_dispatch.py`).
- The AgentTool/SelfAgentTool spec **types** (`omnigent/inner/tools.py`) + their materialization
  into nested `AgentSpec` (`omnigent/spec/omnigent.py`).

**Does NOT own (cross-ref):** the executor turn-loop & SDK event translation (Executor section);
**fork** mechanics (`POST /v1/sessions/{id}/fork` — Server-routes section; fork is a sibling of
spawn but a different flow); runner↔server WS-tunnel transport (Runner/transport section); MCP
routing internals (MCP section); policy evaluation engine (Policy section); claude-native
subagent **bridge file-watch** beyond noting the entrypoint (Native-harness section).

---

## 2. Key files & entrypoints (verified path:line)

| What | Anchor |
|---|---|
| AgentTool / SelfAgentTool **dataclasses** (spec types only — no dispatch) | `omnigent/inner/tools.py:266` (AgentTool), `:298` (SelfAgentTool); fields `pass_history`/`pass_histories`/`max_sessions` `:289-295` |
| `sys_session_send` schema + in-proc tools | `omnigent/tools/builtins/spawn.py:56` (SysSessionSendTool), `:766` (SysSessionCreateTool), `:439` (List), `:1282` (GetHistory), `:1425` (Close) |
| **Spawn dispatch** (mint child Conversation) | `omnigent/runner/tool_dispatch.py:1146` `_execute_subagent_tool`; child create `POST /v1/sessions` `:1435`; message POST `:1538`; handle return `:1567`; by-session-id mode `:1586` |
| `sys_session_create` dispatch | `omnigent/runner/tool_dispatch.py:1828` `_execute_session_create`; body builder `:1730` (`parent_session_id` hard-forced `:1755`) |
| Inbox drain (`sys_read_inbox`) | `omnigent/runner/tool_dispatch.py:5476` `_drain_inbox`; delayed-policy eval `:5409`; consume-once via `get_nowait` `:5500` |
| Completion push into parent inbox | `omnigent/runner/app.py:7248` `_deliver_subagent_completion` (`inbox.put_nowait` `:7279`); wake notice `:7456` `_format_subagent_wake_notice` |
| Child→parent fan-out registry | `omnigent/runner/app.py:7041` `register_subagent_work`; `_session_inboxes_ref` `:7692`; queue create `:8888` |
| Child turn spec swap (own-subagents init) | `omnigent/runner/app.py:8717-8728` (`_find_spec_by_name(spec, sub_agent_name)`) |
| Sub-agent spec resolution + web-researcher fallback | `omnigent/runtime/workflow.py:2546` `_find_spec_by_name`; pure search `:2639` `_search_sub_agent_tree` |
| SelfAgentTool clone + parse-time recursion guard | `omnigent/spec/omnigent.py:1252` `_build_self_clone_subspec`; strip-self `:1307-1310` |
| Server agent cache (2-tier) | `omnigent/runtime/agent_cache.py:16` AgentCache; `load` `:51`; warm-swap `replace` `:102`; `evict` `:161` |
| Runner bundle disk cache (separate) | `omnigent/server/bundles.py:561` `_agent_cache_dest`; download+parse `:645-656` |
| `Agent` DB entity (3rd tier) | `omnigent/entities/agent.py:11` (`bundle_location` `:36`, `version` `:37`, `session_id` `:40`) |
| Smart routing | `omnigent/server/smart_routing.py:215` `route_turn`; `MODEL_LISTS` `:25`; `_HARNESS_FAMILY` `:49`; `infer_models` `:62`; LLM judge `:155` |
| route_turn call site (first-message gate) | `omnigent/server/routes/sessions.py:8650-8684` |
| Resume dispatch | `omnigent/resume_dispatch.py:39` `run_resume`; wrapper→harness `:201` `_dispatch_wrapper` |
| `sys_cancel_task` (no-op) | `omnigent/tools/builtins/async_inbox.py:97-126` |
| Spawn-depth constant (display-only) | `omnigent/repl/_repl.py:201` `_MAX_SUBAGENT_TREE_DEPTH=3`; used `:6271` (`_refresh_subagent_tree` sidebar poll) |

---

## 3. Internal model

**Sub-agent identity & tree.** A named sub-agent is a normal Omnigent Conversation row with:
- `title = "<agent>:<title>"` (e.g. `"worker:banana"`), the composite the runner scans for
  spawn-vs-continue (`tool_dispatch.py:1404`, `_find_existing_child_session:837`).
- `parent_conversation_id` = the **immediate** caller (Phase-4; nested sub-agents point at their
  spawning sub-agent, not the root — `spawn.py:407` `_resolve_parent_conversation_id`).
- `root_conversation_id` = the spawn-tree root (NOT NULL post-migration `d8e2f3b4c910`). **All
  tree-scoping (peek/close) is enforced by `root_conversation_id` match**, not parentage
  (`spawn.py:1197`).
- `sub_agent_name` column (migration `f1a2b3c4d5e6`) — the dispatched sub-agent's name; the runner
  uses it to resolve the child's spec from the parent bundle.

**Close = tombstone, not delete.** `sys_session_close` rewrites title to
`"<agent>:<title>:closed:<conv_id>"` + sets label `omnigent.closed=true`
(`spawn.py:1530-1535`). Items survive; the `(agent,title)` lookup no longer matches so a re-send
spawns a fresh child.

**Per-session inbox.** `_session_inboxes_ref: dict[conv_id, asyncio.Queue]` on the runner
(`app.py:7692`). A queue is created lazily per session (`app.py:8888`). Sub-agent completions and
`sys_call_async` results are `put_nowait`'d here; drained consume-once by `sys_read_inbox` or by
the loop's between-iteration auto-collect.

**Storage = 3 tiers** (see §6 storage answer). `Agent` row carries
`bundle_location` (content-addressed `<agent_id>/<sha256>`), monotonic `version`, and
`session_id` (None = operator/template agent; non-null = session-scoped tenant agent — gates
`expand_env`).

**AgentCache tiers** (`agent_cache.py`): Tier-1 in-memory `_specs: dict[agent_id, AgentSpec]`,
Tier-2 disk `<cache_dir>/<agent_id>/`, source-of-truth = ArtifactStore tarball. **No TTL** — only
explicit `evict()` (on delete) and `replace()` (warm-swap on update). The runner keeps a
**separate** disk cache keyed by `(agent_id, version)` (`bundles.py:561`).

---

## 4. Inter-component channels (in/out edges)

All durable state is the conversation/agent rows; SSE/queue events are transient.

```
 ORCHESTRATOR turn (claude-sdk)        RUNNER (tool_dispatch)            SERVER (REST)
 ─ LLM emits sys_session_send ──────▶ _execute_subagent_tool
   (action_required SSE)               │ POST /v1/sessions ───────────▶ create child conv row
                                       │   {agent_id=PARENT, parent_   (durable; SessionResponse)
                                       │    session_id, title=a:t,
                                       │    sub_agent_name}
                                       │ register_child_session +
                                       │   register_subagent_work (local)
                                       │ publish session.created ─────▶ relay → parent SSE stream
                                       │ POST /v1/sessions/{child}/      (server forwards to runner,
                                       │   events  (user message) ────▶  starts child turn)
                                       │ return handle{status:launching}
   LLM gets handle ◀───────────────────┘
   ... parent turn ends or yields ...
                          child turn completes on runner →
                          _deliver_subagent_completion: inbox.put_nowait({type:sub_agent,...})
                          + wake POST  ─────────────────▶ parent /events ▶ relaunch parent turn
   LLM calls sys_read_inbox ─────────▶ _drain_inbox: get_nowait,
                                       │  _evaluate_subagent_inbox_output (delayed policy)
   "[System: ...BANANA]" ◀────────────┘  consume-once
```

**Trace evidence (`conv_fc47380ccbff481abf452a446ec4e40d`, summary edges):**
- `tool:sys_session_send` (payload: `{"agent":"worker","title":"banana","args":{"input":"Reply
  with exactly the word BANANA...","purpose":"implement"}}`) — orchestrator→worker dispatch.
- `omni-server -> omni-runner [POST /v1/sessions] x4` — child Conversation minting (the child is
  `conv_263ec4375e7f4ec796c39699ce9fcbda`).
- `omni-runner -> omni-server [GET /v1/sessions/{id}/child_sessions] x1` — the spawn-vs-continue
  existence check (`_find_existing_child_session`).
- `omni-server -> omni-runner [POST /v1/sessions/{conv}/events] x3` — child user-message + parent
  wake.
- Wake notice payload captured verbatim: `"[System: sub-agent worker/banana finished (completed)
  — 1 result waiting in inbox. Call sys_read_inbox to collect.]"` → matches
  `app.py:7471` exactly.
- `tool:sys_read_inbox x2` — first returns `"Inbox is empty — no completed tasks."` (drain before
  child finished), second returns the BANANA result `"[System: sub-agent task
  conv_263ec...completed — worker:banana returned: BANANA]"`. Confirms consume-once + the
  empty-inbox sentinel (`_drain_inbox:5495`).

**Channels in summary:**
- Spawn / create / message / child-enumeration: **runner→server REST** over the WS tunnel
  (`server_client` httpx). Durable: conversation rows. Transient: `session.created` /
  `child_session.updated` SSE on the parent stream.
- Result return: **runner-internal `asyncio.Queue`** (no network) + a **wake POST** runner→server
  →runner to relaunch the parent turn so it drains.
- `sys_session_send` itself reaches the runner as an **Omnigent MCP tool** (`POST /mcp` +
  `/mcp/execute` in the trace) — the orchestrator's harness sees it as a normal tool call;
  `should_dispatch_locally` routes it to `_execute_subagent_tool` (`tool_dispatch.py:204,497`).

---

## 5. CUJ behaviors (per harness/client)

**Spawn-or-continue (named mode), all harnesses with the Omnigent tool surface.**
1. LLM calls `sys_session_send(agent, title, args)`. `args` may be a bare string or
   `{input, purpose, model, harness, cost_budget}` (object form; `purpose` is what Polly's
   guardrail keys on — `tool_dispatch.py:878`).
2. Runner resolves parent `agent_id` (local cache → `GET /v1/sessions/{id}`), checks for an open
   child by `(agent,title)`, creates one if absent with `agent_id = PARENT's id` (inline
   sub-agents share the parent bundle — `:1402`), posts the user message, returns a `launching`
   handle.
3. Child runs on the **same runner** (affinity; `parent_session_id` co-locates it). At child-turn
   start the runner **swaps `spec` to the child's own sub-spec** via
   `_find_spec_by_name(parent_spec, sub_agent_name)` (`app.py:8717`) — this is what gives the
   child its own harness/model/tools/os_env/**sub_agents**.
4. On completion the runner pushes the payload into the parent's inbox queue and wakes the parent;
   the parent drains via auto-collect or `sys_read_inbox`.

**By-session-id mode** (`session_id` arg): posts to an **existing direct child only** — verified
by `parent_session_id == caller` (`_send_to_existing_session:1629`). Cross-tree / sibling sends
→ `session_out_of_tree`.

**`sys_session_create`**: spawns a child from an `agent_id` (any agent the caller can see) or a
local `config_path` (uploads a fresh **session-scoped** agent). `parent_session_id` hard-forced to
caller. Unlike named send, does **not** block on the child turn (`tool_dispatch.py:1828`).

**Parallel fan-out:** multiple `sys_session_send` calls in one assistant response dispatch
concurrently; each completion lands independently in the inbox.

**TUI vs WebUI:** identical spawn mechanics. The difference is **discovery rail rendering** —
the REPL's `_refresh_subagent_tree` recurses `GET …/child_sessions` to depth
`_MAX_SUBAGENT_TREE_DEPTH=3` (`_repl.py:6266`); the SSE stream only delivers the **active
session's direct children** (`_repl.py:6247`), so grandchildren are kept live only by this poll.
WebUI has its own `SubagentsPanel.tsx`/`useChildSessions.ts`.

**⚠️ Failure branches (code-confirmed):**
- ⚠️ **Spec-resolution miss → parent-clone fallback (runaway recursion).** If
  `sub_agent_name` doesn't resolve in the parent tree (and isn't the web-researcher), callers
  keep the **parent spec** and boot the child as a full clone of the parent — explicitly called
  out as "runaway recursion via `sys_session_send` when the parent is a coordinator"
  (`workflow.py:2563-2566`).
- ⚠️ **No spawn-time depth cap** (§6 — only the display poll is bounded).
- ⚠️ **`sys_cancel_task`/`sys_cancel_async` cancel sub-agents on the runner path** (via
  `_cancel_subagent_task`, `tool_dispatch.py:5764`) but the in-process `SysCancelTaskTool.invoke`
  returns `task_not_found` for everything (`async_inbox.py:97-126`); a handle the work-registry
  doesn't recognize gets the misleading no-op + "tasks table removed" hint.
- ⚠️ **Missing parent inbox** → completion logged + dropped, returns 503
  `subagent_delivery_not_confirmed` (`app.py:7264-7275, 7441`); the runner backfills
  `sub_agent_name` from the server snapshot on reconnect to limit this (`app.py:10291`).
- ⚠️ **`busy`/`launching`/`running` child** → re-send rejected (`sub_agent_busy`), caller must
  wait for the drain or cancel (`tool_dispatch.py:1318-1332`).
- ⚠️ Native sub-agent completions (claude-native) historically never reaching the orchestrator is
  a known field issue (CUJ-ANALYSIS §2.F failures) — owned by the Native-harness section; the
  inbox/queue path above is the SDK-harness path.

---

## 6. Answers to the doc questions (code-anchored)

**How subagents are spawned.** The LLM calls the Omnigent tool `sys_session_send`/`create` (NOT a
per-`AgentTool` named tool — AgentTool/SelfAgentTool are *spec types* that translate into nested
`AgentSpec` sub_agents; the LLM-facing dispatch is always the generic `sys_session_send` with a
dynamic `agent` enum, `spawn.py:185`). The runner intercepts it
(`should_dispatch_locally`→`_execute_subagent_tool`), mints a child Conversation via
`POST /v1/sessions` with the **parent's** `agent_id` + `parent_session_id` + `title="<a>:<t>"` +
`sub_agent_name`, posts the first user message, and the child runs the same workflow loop on the
same runner. (`tool_dispatch.py:1146`.)

**Info propagation parent↔child (#5).** *Type-level:* `AgentTool.pass_history=True` snapshots the
parent's `"self"` history into the child as `"parent"`; `pass_histories=[names]` snapshots named
histories (`inner/tools.py:289-294`). *Runtime via `sys_session_send`:* the only thing crossing is
`args.input` (the child's first user message); the child's result is truncated and packaged into
the inbox payload (`output` field, `app.py:7290`). **Siblings/cross-agent never talk directly —
only through the parent.** A parent can *read* a child's transcript with
`sys_session_get_history` and *drive* a child with `sys_session_send`/`session_id`, both
**tree-scoped** to `root_conversation_id`.

**Subagent depth limits — ⚠️ confirmed gap.** There is **no spawn-time depth cap anywhere** (grep
of `tool_dispatch.py`, `spawn.py`, `server/routes/sessions.py` for depth/recursion checks → none).
`_MAX_SUBAGENT_TREE_DEPTH=3` is **display-only** (sidebar poll recursion bound, `_repl.py:6271`).
The only structural guard is `AgentTool.max_sessions` (optional per-tool concurrency cap on named
children, surfaced in the tool description `inner/tools.py:161`) and `SelfAgentTool`'s
**parse-time** strip (`spec/omnigent.py:1307-1310`) which prevents an infinite *parse-time* clone
tree but explicitly **allows runtime recursion** ("Recursion at runtime (a clone spawning another
clone) IS [possible]", `spec/omnigent.py:1284-1287`). So a coordinator agent that self-spawns can
recurse without bound.

**How a custom agent's OWN subagents get initialized.** They are **nested `AgentSpec` nodes in the
same bundle** — not separate registrations. When the runner starts a child turn it deep-resolves
the child's spec out of the parent bundle via `_find_spec_by_name(parent_spec, sub_agent_name)`
(`app.py:8721`) and swaps `spec` to that sub-spec; the sub-spec carries its *own* `sub_agents`, so
the grandchild surface comes for free at the next spawn. Inline sub-agents therefore all share the
parent's single `agent_id`/bundle. `AgentTool` can also reference a separately-registered agent
**by name**, and `SelfAgentTool` clones the parent def minus self-tools.

**How custom agents are stored (3 tiers) + caching.**
1. **ArtifactStore** — content-addressed `.tar.gz` (key `<agent_id>/<sha256hex>`); source of
   truth.
2. **`Agent` DB row** (`entities/agent.py`) — `id`, `name`, `bundle_location`, monotonic
   `version` (bumps on update), `session_id` (None=template/operator agent → `expand_env=True`
   allowed; non-null=session-scoped tenant agent → `expand_env=False`, no `${VAR}` expansion to
   avoid secret leak, `agent_cache.py:74-80`).
3. **Server `AgentCache`** (`runtime/agent_cache.py`) — Tier-1 in-mem `_specs` dict + Tier-2 disk
   extract. **No TTL.** Invalidation is explicit: `evict()` on delete, `replace()` warm-swap on
   update (extract to staging, atomic dict assignment, `rename` into place so readers see old-or-
   new, never empty, `:133-159`). Execution load uses `prune_invalid_sub_agents=True` (version
   skew → drop unknown sub-agent with a WARNING rather than fail the parent, `:30-34`).
   The **runner** has an independent disk cache keyed by `(agent_id, version)`
   (`bundles.py:561`) — two distinct caches.
Created via `omnigent create` / POST bundle; uploads are validated and **reject server-side
`callable:` Python tools** (GHSA RCE guard, `bundles.py:29` `_reject_uploaded_callable_tools`,
recurses sub-agents).

**Intelligent routing (`route_turn`).** `server/smart_routing.py:215`. Fires server-side at
`sessions.py:8650` **only when** the session toggle `cost_control_mode_override=="on"`, no model
chosen yet (`effective_runner_override is None`), and `body.type=="message"` (effectively the
first turn; verdict is persisted as `model_override` so later turns skip the judge). Flow:
`infer_models(harness)` → harness family (`claude`/`gpt`/`pi`) → an **LLM judge**
(`LLMRoutingClient`) that reasons about model *names* directly (haiku<sonnet<opus, nano<mini<base,
higher version = newer) and returns `{model, rationale}` strict-JSON. **No `TIER_TEMPLATES`** — it
picks from the flat per-family `MODEL_LISTS` (`:25`). Fail-open: judge error → `None` (spec
default); hallucinated model → clamp to `available_models[0]` (`:198`). Sub-agents inherit the
parent's routing toggle (`sessions.py:8642-8652`).

**Resume dispatch (which harness gets re-launched).** `omnigent/resume_dispatch.py:39` —
`omnigent resume <conv>` (or picker) reads the conversation's `labels.omnigent.wrapper` (local
store or remote `GET /v1/sessions/{id}`) and re-launches the matching **terminal-native** wrapper:
`_dispatch_wrapper` maps the wrapper label → `run_claude_native` / `run_codex_native` /
`run_pi_native` / cursor / kiro / goose / antigravity / qwen / kimi / hermes (`:201-309`). **Only
terminal-native sessions resume in-process**; anything else (SDK sessions) surfaces a
copy-pasteable `omnigent run --resume <conv> <agent.yaml>` hint (`:179-198`). This file is CLI glue
only — the *transcript reload into the runner* is the Runner/resume section.

**Inbox / async-work mechanics.** Three LLM tools (`tools/builtins/async_inbox.py`), gated on
spec `async:` (defaults **True** — `datamodel.py:782`, `spec/types.py:1537`):
- `sys_call_async(tool, args)` — dispatches a **local Python tool** as a background `asyncio.Task`
  on the runner (`tool_dispatch.py:5529` `_spawn_async_tool`), returns a handle immediately;
  result `put_nowait`'d to the session inbox on completion. (MCP/builtin/client tools and
  `sys_call_async` itself are rejected.)
- `sys_read_inbox()` — non-blocking consume-once drain of the queue (`_drain_inbox:5476`); each
  sub-agent payload passes a **delayed policy eval** (`_evaluate_subagent_inbox_output:5409`,
  fail-closed to a suppression sentinel) before formatting; empty → `"Inbox is empty — no
  completed tasks."` Also auto-collected at every loop-iteration boundary so the LLM sees
  completions even without calling it.
- `sys_cancel_async(handle_id)` / `sys_cancel_task(task_id)` — `sys_cancel_async` cancels a live
  async `asyncio.Task` or routes to the sub-agent work registry; the bare `sys_cancel_task`
  **always returns `task_not_found`** (tasks table removed, `async_inbox.py:97`).

---

## 7. Reliability gaps / sharp edges (code-confirmed)

1. **No spawn-time recursion/depth bound.** A coordinator agent with a `self` sub-agent (or one
   whose `sub_agent_name` misresolves → parent-clone fallback) can spawn unboundedly deep. Only
   `max_sessions` (per-tool concurrency) and the display poll (depth 3) constrain anything.
   (`workflow.py:2563`, `_repl.py:201`.)
2. **`sys_cancel_task` in-process `invoke` is a no-op**, but the *runner* dispatch path is not.
   `should_dispatch_locally` routes `sys_cancel_task`/`sys_cancel_async` to
   `_cancel_async_tool` → falls through to `_cancel_subagent_task` for sub-agent handles
   (`tool_dispatch.py:5755-5764`), so a sub-agent CAN be cancelled on the runner. The
   `SysCancelTaskTool.invoke` body that returns `task_not_found` unconditionally
   (`async_inbox.py:97-126`) only runs on the in-process (non-runner) path — but that means a
   bare async-tool handle (`sys_call_async`) whose `asyncio.Task` already finished, or any id the
   work-registry doesn't know, gets `task_not_found` with a misleading "tasks table removed" hint.
3. **Inbox is a process-local `asyncio.Queue` with no durable backing.** If the parent's runner
   restarts before a child completion is drained, the queued payload is lost (the row is durable
   but the *delivery signal* is not); `_deliver_subagent_completion` only warns + returns
   `MISSING_PARENT_INBOX` 503 when the queue is absent (`app.py:7264`). Reconnect backfills
   `sub_agent_name` (`app.py:10291`) but not pending inbox payloads.
4. **Spawn-vs-continue scans up to 1000 children and filters locally** — the child-session
   endpoint has no `(tool, session_name)` filter; an agent with many named children pays an
   O(n) fetch per send (`tool_dispatch.py:849-853`).
5. **Per-spec `allowed_harnesses` allowlist for `args.harness` is enforced ONLY at orchestrator
   dispatch** (`tool_dispatch.py:1340-1369`). A direct `POST /v1/sessions` with `harness_override`
   is bounded only by the **global** `OMNIGENT_HARNESSES`, not the per-spec allowlist — explicitly
   noted in-code (`tool_dispatch.py:1340-1345`).
6. **AgentCache has no TTL or size bound** — `_specs` + disk dirs grow until explicit
   `evict`/`replace`. A long-lived server accumulates every agent it ever loaded
   (`agent_cache.py`).
7. **Two independent agent caches** (server `AgentCache` vs runner `bundles.py` per-version dir)
   can diverge on version skew; the runner's is keyed by `(agent_id, version)` so a warm-swap on
   the server doesn't invalidate the runner's copy of the old version.

---

## 8. Corrections to CUJ-ANALYSIS §2.F

The §2.F summary is mostly right; these claims are wrong or drifted:

1. **`TIER_TEMPLATES` does not exist.** §2.F line 317: "picks a model from `TIER_TEMPLATES`".
   `smart_routing.py` has **no tier abstraction** — it uses flat per-family `MODEL_LISTS`
   (`:25`) and an LLM judge that reasons about model *names* directly. Module docstring even says
   "no tier abstraction" (`smart_routing.py:7-9`). Hallucinated model clamps to
   `available_models[0]`, not `tier[0]`.
2. **"native harnesses not routable (returns None)" is WRONG.** §2.F line 318. `_HARNESS_FAMILY`
   maps `"claude-native":"claude"` and `"codex-native":"gpt"` (`smart_routing.py:51-58`), so
   `infer_models` returns a model list for native harnesses and `route_turn` runs. The real
   nuance (worth stating instead): native harnesses bake `--model` at terminal launch, and the
   routing call only fires on a **message** event with the toggle on, so a routed model is
   persisted as `model_override` but a native session created without it won't pick it up
   mid-stream.
3. **Spawn is `sys_session_send`, not a per-`AgentTool` named tool.** §2.F line 306 implies "LLM
   calls a sub-agent tool → mints a child Conversation". Accurate mechanism: the LLM always calls
   the generic `sys_session_send` (dynamic `agent` enum); `AgentTool`/`SelfAgentTool` are spec
   *types* translated into nested `AgentSpec.sub_agents`. The mint happens on the **runner** via
   `POST /v1/sessions` using the **parent's** `agent_id` — inline sub-agents are not separately
   registered. (CUJ's `inner/tools.py:267,298` anchors are correct for the *type defs*; they are
   NOT the dispatch site — dispatch is `tool_dispatch.py:1146`.)
4. **`SelfAgentTool` prune stops *parse-time* recursion only, not runtime.** §2.F line 313:
   "pruned from the clone to stop `self`-recursion". The strip (`spec/omnigent.py:1307-1310`)
   prevents an infinite *parse-time* clone tree; runtime recursion (a clone spawning another
   clone) is **explicitly still possible** (`spec/omnigent.py:1284-1287`). The §2.F "no
   spawn-time depth cap" conclusion is correct; the SelfAgentTool clause overstates the guard.
5. **`resume_dispatch.py` is at `omnigent/resume_dispatch.py`, not `omnigent/runtime/`** (the
   briefing's anchor path). CUJ §2.F line 337 cites `resume_dispatch.py:39` without a dir and is
   effectively correct; flagging because the task brief pointed at `runtime/resume_dispatch.py`
   which does not exist.

(Confirmed-correct §2.F claims worth keeping: 3-tier storage + "no TTL, evict on delete, warm-swap
on update"; `pass_history`/`pass_histories`; siblings-only-via-parent; `async_work_complete`
consume-once; `sys_cancel_task` no-op `task_not_found`; resume reads the wrapper label.)
