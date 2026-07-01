# CUJ answers — Executor · Subagents · Inbox · Compaction · Resume · Custom-agent storage · Caching

Domain: the **runtime turn loop & agent machinery** (`omnigent/runtime/` +
`omnigent/inner/{executor,tools,tracing}.py` + `tools/builtins/{spawn,async_inbox}.py`).
Companion architecture doc: `../architecture/runtime-executor.md`.
Source = code (`file:line`); validated with the local Jaeger rig. In scope:
claude-sdk, codex, polly. codex has **no live trace** (gateway creds 403).

---

## Q1. Role of the executor in the turn loop

An **Executor** (`inner/executor.py:518`) is the per-vendor adapter that
translates **Omnigent's abstract turn model ↔ a concrete vendor SDK**. The
abstract model is deliberately tiny:

- **In:** `run_turn(messages: list[Message], tools: list[ToolSpec],
  system_prompt: str, config: ExecutorConfig)`.
- **Out:** an async stream of `ExecutorEvent`s
  (`inner/executor.py:96-261`): `TextChunk`, `ReasoningChunk`,
  `ToolCallRequest`, `ToolCallComplete`, `TurnComplete`, `CompactionComplete`,
  `TurnCancelled`, `ExecutorError`.

Each vendor (claude-sdk, codex, pi, …) subclasses `Executor` and maps its SDK's
native stream onto this vocabulary. Capability differences are declared via
predicates (`supports_streaming`, `handles_tools_internally`,
`supports_live_message_queue`, `interrupt_session`, `max_context_tokens`, …),
all defaulting ❌ except `supports_tool_calling` (`:541-587`).

**Where it runs:** the executor lives inside a **per-conversation harness
subprocess** (`omni-harness`, `harnesses/_runner.py`). The
**`ExecutorAdapter`** (`harnesses/_executor_adapter.py:141`, a `HarnessApp`
subclass shared by all four SDK wraps) is the bridge:

1. Lazily constructs the inner executor on first turn (`_ensure_executor`).
2. Translates the incoming `CreateResponseRequest` → `Message` list +
   `ExecutorConfig` (`:258-285`).
3. Calls `executor.run_turn(...)` and, per yielded `ExecutorEvent`,
   `_translate_event(...)` emits typed **SSE** events (`response.output_text.delta`,
   reasoning deltas, `response.output_item.done` function_call/output,
   `response.completed/.cancelled`) (`:394-459`).
4. Round-trips spec/MCP tools the SDK can't run itself through
   `dispatch_tool` (action_required → parked Future keyed by `call_id` →
   runner PATCHes the matching `function_call_output`) (`_scaffold.py:449-516`).
5. Wires stable bridges (`_tool_executor`, `_elicitation_handler`,
   `_policy_evaluator`) and the steering watcher (`_watch_injections`).

**Trace evidence (the loop as OpenInference spans):** the executor turn loop is
exactly the `agent:<harness>` AGENT span on `omni-harness` wrapping
`executor.run_turn()`, with tool calls nested under it. Live (corpus
`conv_63542a5f92…` trace `f98feda7…`; fresh `conv_3a411011…` trace `f2e437fa9…`):

```
omni-harness  agent:claude-sdk [AGENT]   (7855ms)   input.value=<prompt>  output.value=<final text>  session.id=conv_…
  omni-harness  tool:sys_os_shell [TOOL]  (208ms)    tool.name=sys_os_shell  input.value={"command":"echo …"}  output.value={"exit_code":0,…}
```

The AGENT span is created by `TracingContext.start_agent_span`
(`inner/tracing.py:130`), the TOOL span by `start_tool_span` on `ToolCallRequest`,
both **only from the adapter** (`_executor_adapter.py:387,413`).

> **Nuance — no `llm_call [LLM]` span for the SDK path.** `tracing.py:26-33`
> *documents* an ideal `agent → llm_call → tool → sub-agent → policy:` nesting
> and `start_llm_span` exists (`:223`), but **no `inner/*_executor.py` calls
> it** — the adapter wraps the whole turn in one AGENT span and the LLM call is
> subsumed. Verified live: zero `llm`-named spans in Jaeger. Real nesting for
> claude-sdk is **agent → tool**; guardrails appear as a separate
> `policy.evaluate` span on `omni-server`; a sub-agent's AGENT span is a
> separate omni-harness-rooted trace.

---

## Q2. How subagents are spawned (+ depth limits)

**Two declaration forms** (parse-time → nested `AgentSpec` in `sub_agents`,
`spec/omnigent.py:1090-1120`):
- **`AgentTool`** (`inner/tools.py:266`) — a sub-agent referenced **by name**
  (registered agent) or **inline**. `_agent_tool_to_sub_spec`
  (`spec/omnigent.py:1319`) synthesizes the child spec, inheriting parent
  profile/harness/os_env/terminals and recursing into nested tools.
- **`SelfAgentTool`** (`inner/tools.py:298`; `tools.<name>: self`) — clones the
  **parent's** spec (`_self_agent_tool_to_sub_spec`), with all `SelfAgentTool`
  entries **removed from the clone** (`spec/omnigent.py:1310`) as the
  self-recursion guard.

**Runtime spawn (LLM-driven, SDK harnesses):** the LLM calls
**`sys_session_send`** (`tools/builtins/spawn.py:56`):
- mode A `(agent, title)` → **mints a child Conversation** and starts a turn;
- mode B `session_id` → posts to an **existing direct child**.
The child's `parent_session_id` = the **immediate parent**
(`_resolve_parent_conversation_id`, `spawn.py:407`); it **inherits the caller's
runner** (co-location) and **runs the same turn loop** (its own omni-harness
ExecutorAdapter → its own AGENT-rooted trace). Results return to the parent via
the `async_work_complete` inbox signal (Q4). For claude-**native**, children are
minted via `external_subagent_start` instead (native SME).

**Depth limits — VERIFIED display-only, NO spawn-time cap.**
- `_MAX_SUBAGENT_TREE_DEPTH = 3` (`repl/_repl.py:201`). **Sole use:**
  `_refresh_subagent_tree(max_depth=_MAX_SUBAGENT_TREE_DEPTH)`
  (`_repl.py:6266-6296`) → caps how deep the **REPL sidebar renders** the child
  tree ("mirrors web's MAX_TREE_DEPTH"). It is a **rendering cap on
  `child_sessions_tree`**, never consulted at spawn time.
- No spawn-time depth check exists anywhere. `SelfAgentTool` recursion is
  bounded only by clone-pruning (one level of `self`); ordinary `AgentTool`
  chains can recurse unbounded. `AgentTool.max_sessions` (`inner/tools.py:295`)
  is an optional per-tool **concurrency** cap, not depth.
- **Gap:** real runaway-recursion risk (CUJ-ANALYSIS §6, "add when needed").

---

## Q3. Info propagation parent↔child

- **`pass_history: true`** snapshots the parent's `"self"` history into the
  child as `"parent"` history; **`pass_histories: [names]`** snapshots named
  histories (`inner/tools.py:281-294`). The child executor serializes that
  passed history into its context on the **first turn**
  (`claude_sdk_executor.py:2600`, `cursor_executor.py:223`,
  `pi_executor.py:1233`).
- **Tool args = the child's first user message** — `sys_session_send`'s
  `args.input` is "treated as the first user turn in its conversation"
  (`spawn.py:281`).
- **Results truncated into the inbox signal** — on child turn completion the
  output is **truncated** to a per-payload char budget (matching the `@tool`
  path's `truncate_for_llm`, `workflow.py:94-104`) and packaged into the
  `async_work_complete` payload the parent drains.
- **Siblings / cross-agent only via the parent** — `sys_session_send` is
  "confined to your direct children" (`spawn.py:73-74,245`); a child addresses
  only its own children or its parent. No sibling channel.
- **Lossy on the omnigent-compat translator:** `pass_history` /
  `pass_histories` / `max_sessions` are dropped to defaults when an agent goes
  through `spec/omnigent.py` (`:1338-1341`); first-class on the inner
  `AgentDef` path (`inner/loader.py:450-519`).

---

## Q4. Inbox / async mechanics

- **`sys_call_async(tool, args)`** (`tools/builtins/async_inbox.py:129`) —
  dispatch a **local Python tool** as a background task; `is_async()` is always
  True (`:222`) → routes to `dispatch_async`, returns an **`_AsyncToolHandle`**
  JSON `{task_id, tool_name, status:"in_progress", message}`
  (`workflow.py:2143`, `to_handle_json`). MCP/builtin/client tools and a
  recursive `sys_call_async` target are rejected.
- **Where the blocking drain lives:** in the **harness subprocess**, not the
  runner. Heartbeat `_HEARTBEAT_INTERVAL_S=15.0` (`_scaffold.py:83`;
  `_heartbeat_loop`→`response.heartbeat`, `:1542`); steering
  `TurnContext.next_injection(timeout=…)` (`:551`); auto-collect
  `_drain_async_completions` (`async_inbox.py:253`). (The `workflow.py:94-118`
  constants are stripped doc-comments only.)
- **Drain — two paths, both consume-once:**
  - **Auto-drain at the iteration boundary (push):** `_drain_async_completions`
    runs at the **top of every loop iteration**, delivering piled-up
    `async_work_complete` payloads as `[System: task …]` user messages
    (`async_inbox.py:253-261`).
  - **`sys_read_inbox` (pull, mid-turn):** the LLM drains the inbox inline as a
    `function_call_output` without waiting for the iteration boundary — used to
    fan out a second wave based on first-wave results (`:240-308`). Like
    auto-collect it's `is_async()=True` but returns a string, not a handle
    (`:310-331`).
  - **Consume-once:** both remove payloads off the topic, so the next
    iteration's auto-collect never re-delivers — the LLM never sees a completion
    twice (`:264-266`).
- **Topic:** `async_work_complete` (`workflow.py:2160`) — the signal sub-agent
  completions ride on. **Subagent-completion delivery (runner side)** runs in
  `_on_proxy_stream_end` (`runner/app.py:12519`), pushing the child's output to
  the **parent's inbox queue** (`_deliver_subagent_completion`, `:7248`) +
  scheduling a parent wake-POST (`:12957`), gated at **`runner/app.py:12607`**
  `elif not _is_native_harness(conv_id) and not has_buffered:` (⚠️ native
  children skip this; #848).
- **`sys_cancel_task` / `sys_cancel_async` — NO-OP (confirmed).** The tasks
  table was removed; `SysCancelTaskTool.invoke` returns
  `{"error":"task_not_found","hint":"The tasks table has been removed…"}`
  **for every input** (`async_inbox.py:97-126`). `sys_cancel_async` subclasses
  it with a `handle_id` alias → identical no-op (`:334-412`). Async/subagent
  cancellation is effectively broken. (Per docstring, Bash background jobs are
  killed via the SDK's KillBash instead.)

---

## Q5. Compaction / transcript reconstruction

3-layer, least→most lossy (`runtime/compaction.py:544`, `compact()`); budget =
`context_window*0.8 − system_token_budget` (`:615`); recent window = 5 LLM
groups (`:48`):
- **L1 surgical clear** (`:624-631`) — tool-result bodies →
  `"[Previous tool result cleared…]"`; binary `data` →
  `"[binary content removed…use file_id…]"` (keeps `file_id`); strip
  `output_text.annotations`. Returns if L1 fits & not `force`.
- **L2 LLM summary** (`:633-692`) — summarise pre-boundary into a synthetic
  user+assistant pair. **Routed through the runner's `POST /v1/summarize`** when
  a runner client exists (`_summarize_via_runner_uncached`, `:428`) so the
  *runner's* creds are used. 401/403 flagged distinctly (issue #1121, `:761`).
  Publishes `response.compaction.in_progress`/`.completed` SSE (spinner).
- **L3 truncate** (`:696-720`) — emergency oldest-message drop, pair-aware
  (`_pair_aware_drop_count`, `:322`).

**Triggering — two paths (the "runner catches overflow → compacts" belief is
only half right):**
- **Proactive / threshold-based (the real auto-compaction):** runs in the
  **in-process agent loop** (`_call_llm_maybe_compact`, `workflow.py:2057`) →
  `compact(force=False)` when context crosses `trigger_threshold` 0.8
  (`compaction.py:612-631`). `_CompactionState.context_window` learned from the
  first overflow (`:122`). OpenAI-Agents harness delegates to its SDK's
  `run_compaction` (`openai_agents_sdk_executor.py:1137`).
- **Reactive on harness-reported overflow — does NOT auto-compact:**
  `proxy_stream` detects it (`_is_context_overflow_error`, `runner/app.py:6736`)
  → raises `_ContextWindowOverflow` (`:14243`) → caught in `_run_turn_bg`
  (`:13804`) which **ends the turn with an error**, not a compaction. The SDK
  executor re-raises overflow to surface it (`openai_agents_sdk_executor.py:1673`).
  [⚠️ resume-overflow surface, OMNI-143.]
- **Explicit `/compact`:** `compact_conversation_now` (`workflow.py:2347`) →
  `compact(force=True, fail_on_summary_error=True)` (from `sessions.py:10026`).

**Reconstruction:** `_maybe_persist_compaction_item` (`workflow.py:2473`) appends
a `type=compaction` item (idempotent by `response_id==task_id`; refuses broken
items). Next turn/resume, `_load_initial_history` (`workflow.py:2276`) loads only
items **after** `last_item_id` + the expanded summary
(`compaction_to_history_items`, `compaction.py:463`, prefers
`compacted_messages`). **Native:** posts `external_compaction_status`;
`CompactionComplete.compacted_messages` carries the post-compaction transcript
to replay (`inner/executor.py:213-234`). Broken compaction item → ignored, full
conversation reloaded (`workflow.py:2315`).

[memory: compact-every-msg fixed #1082 — the broken-cursor guards in
`_load_initial_history`/`_maybe_persist_compaction_item` are exactly that fix.
⚠️ resume-overflow OMNI-143 — the cursor logic bounds load to O(items since last
compaction), which is the relevant mechanism; whether overflow on resume itself
is fully handled depends on the runner catch — verify with runner SME.]

---

## Q6. Resume — how much transcript loads into the runner

- **SDK (claude-sdk / codex / polly):** the runner reconstructs history via
  `_load_initial_history` (`workflow.py:2276`) — **full conversation, OR (when a
  compaction item exists) only the slice after the last `last_item_id`** plus the
  expanded summary pair — translates it to prompt input, and passes it to
  `executor.run_turn(messages=…)` each turn. The SDK is re-driven from this
  reconstructed history; the in-process SDK client does not persist its own
  store, so "how much loads into the runner" = the (possibly compaction-bounded)
  conversation history.
- **Native (claude-native / codex-native and other native CLIs):** resume keys
  on **`external_session_id`** from the session snapshot (Pi `app.py:728-774`,
  Codex `:841-887`, OpenCode `:973-998`); fork clones seed via
  `FORK_CARRY_HISTORY_LABEL_KEY` (`app.py:743,759,880,986`). The **vendor CLI
  reloads its own session**; Omnigent re-injects only when the vendor store is
  gone — cold resume synthesizes the vendor's local session file from committed
  Omnigent items (Pi `app.py:1715-1729`; OpenCode injects the transcript as a
  preamble, `:1205,1590`). Detail owned by native SME; anchors for completeness.

---

## Q7. Custom-agent storage & a custom agent's own subagents

**Storage — three tiers** (`runtime/agent_cache.py`):
1. **ArtifactStore** — content-addressed `.tar.gz` bundle (source of truth).
2. **Agent DB row** — `id`/`name`/`bundle_location`/`version`/`session_id`
   (session-scoped agents non-null `session_id`; template agents null).
3. **AgentCache** (`agent_cache.py:16`) — Tier 1 in-mem `AgentSpec` (`_specs`)
   + Tier 2 on-disk extract `<cache_dir>/<agent_id>/`. **No TTL.** Miss →
   download → temp file → `load_spec` (extract+parse+validate) → both tiers
   (`load`, `:51`). **Evict on delete** (`evict`, `:161`, drops spec + rmtree).
   **Warm-swap on update** (`replace`, `:102`: extract to `_staging`, atomic
   `_specs[id]=` reassign, rmtree old + rename staging in; readers never see an
   empty cache; version bumps). **Security:** `expand_env=False` default —
   `${VAR}` expanded against server env **only** for operator template agents
   (`session_id is None`), never tenant/session agents (`:66-80`).

**A custom agent's own subagents:**
- `AgentTool` references a registered agent **by name** or an **inline** spec;
  `SelfAgentTool` clones the parent (self-tools removed) — both → nested
  `AgentSpec` (`spec/omnigent.py:1090-1120`).
- Loaded with **`prune_invalid_sub_agents=True`** (`agent_cache.py:94,146,204`;
  impl `spec/__init__.py:314`): depth-first, a sub-agent that fails validation
  on **this** (older) server is dropped (WARNING) and its name removed from the
  parent's `tools.agents`, so version skew degrades gracefully and the parent
  still dispatches. Authoring/upload validation stays strict elsewhere.

---

## Q8. Caching — agent cache + credential cache (table)

| What | Where (file:line) | TTL | Invalidation |
|---|---|---|---|
| **Agent bundle** (parsed spec + extracted dir) | `runtime/agent_cache.py:16` (`_specs` + `<cache_dir>/<agent_id>/`) | **none** | explicit `evict()` on delete; `replace()` warm-swap on update (version bump) |
| **Provider model listing** (`sys_list_models` backing) | `model_catalog.py:61` `_CATALOG_TTL_S=300.0`; `TTLCache(maxsize=64)` `:203` | **5 min** | TTL expiry; `clear_model_catalog_cache()` (`:250`) |
| **MLflow model catalog** (per provider) | `onboarding/providers/__init__.py:102` `_CATALOG_TTL_SECONDS=3600`; `TTLCache(maxsize=64)` `:103` | **1 h** | TTL expiry |
| **Provider/credential resolution** (auth/base-url/profile) | resolved per call in `workflow._resolve_provider_for_build` (`:996`) and the per-harness spawn-env builders | **none** | recomputed fresh each spawn (nothing persisted) |
| **Inner LLM client singleton** | `workflow.py:251` `_llm_client` | process-lifetime | none (lazy singleton; used for server-side L2 summarize fallback) |
| **Native session state / policy-hook token** | `bridge.json` / `policy_hook.json` (native; out of `runtime/` scope) | one-shot snapshot | re-created on relaunch (→ known stale-token fail-closed bug, §2.G) |

Notes: the **credential cache is essentially "none" inside `runtime/`** — LLM
auth, base URL, profile are resolved fresh per spawn from the spec/provider
config, and per-request token refresh lives in the executors / runner auth
callbacks (chat-path refreshes per request; the native policy-hook snapshot is
the one that doesn't — covered by the auth SME). The two real caches are the
**agent cache (no TTL, event-invalidated)** and the **model-catalog caches
(5 min / 1 h TTL)**.

---

## Failure branches & gaps (this domain)

- **No spawn-time subagent depth cap** — `_MAX_SUBAGENT_TREE_DEPTH=3` is
  REPL-render-only (`repl/_repl.py:201`).
- **`sys_cancel_task`/`sys_cancel_async` no-op** — tasks table removed
  (`async_inbox.py:108`).
- **Native sub-agent completions never reach the orchestrator** — gate
  `elif not _is_native_harness(conv_id) and not has_buffered:`
  (`runner/app.py:12607`) excludes native harnesses from the `status="completed"`
  delivery; native turns only emit `waiting`/`idle` (`:12580`). #848 cluster;
  cross-domain w/ native SME.
- **Reactive overflow ≠ compaction** — harness-reported overflow ends the turn
  with an error (`runner/app.py:13804`); only the proactive threshold path
  auto-compacts (`workflow.py:2057`). OMNI-143 surface.
- **`pass_history`/`pass_histories`/`max_sessions` lossy** on the omnigent-compat
  path (`spec/omnigent.py:1338`).
- **No `llm_call` span** → per-LLM-call latency/usage not separately observable
  (usage recorded on the AGENT span, `_executor_adapter.py:435`).
- **Compaction L2 auth failure** silently degrades to lossy L3 (distinct ERROR
  log, turn proceeds; `compaction.py:844`).
