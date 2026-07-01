# Live-trace findings — what the traces reveal beyond the code analysis

Rig: local Jaeger `:16686`, persistent server `:7777` + host (telemetry → `:14317`).
Query by `session.id` via `scratchpad/trace_tools.py`. claude-sdk = primary;
codex family blocked (creds — host reports `codex: needs-auth`).

## Fork — `conv_820c6dee` → `conv_7fe12aec`
- `POST /v1/sessions/{source_id}/fork` is **server-side only**: new conv returns with
  `runner_id=null`, `host_id=null` — no runner/harness spawned for the copy.
- Trace (under the **source's** `session.id`, path-based tag): a burst of **~25 `INSERT`
  + many `SELECT` on `chat.db`** = the synchronous **deep-copy of conversation items**.
- New session gets a **cloned agent** (new `agent_id`) + label `omnigent.fork.source_id`;
  instance-scoped labels dropped.
- **Beyond code:** fork cost is synchronous server DB work that **scales with item count**
  (INSERT-per-item) — visible as a latency/■span spike, not a runner concern.

## Switch-agent — `conv_820c6dee` trace_probe → debby
- `POST /v1/sessions/{id}/switch-agent {agent_id}` on an **idle** session returns
  immediately; binds a **cloned** target agent (`ag_3f172efb`), **resets labels**, and
  **retains the runner binding** (`runner_token_c2a963…`).
- **Beyond code:** the runner binding survives the switch; the next turn reuses the same
  runner, which re-initializes for the new agent/harness (no new runner launch on switch).

## Interrupt — long claude-sdk bash turn cancelled mid-flight (`conv_7fe7513654`)
- Delivered as a **control event**: `POST /v1/sessions/{id}/events {"type":"interrupt"}`
  (same endpoint as input; returns `{"queued":false}` = applied immediately, not queued).
- **Concrete trace signal:** the `omni-harness agent:claude-sdk` span carries
  **`error.type=cancelled`** (the `record_cancellation` helper in `runtime/telemetry.py`)
  while `otel.status_code` stays OK — the unambiguous "this turn was interrupted" marker.
- The in-flight tool call was abandoned mid-setup (the turn had reached `POST /mcp` +
  `policy.evaluate` (TOOL_CALL) + `POST /mcp/execute` for the `sleep 30` bash when cut).
- An `omni-server GET … ERROR` shows at teardown (stream/tunnel close on interrupt —
  consistent with interrupt-fencing dropping trailing output).
- **Beyond code:** the trace fingerprint of an interrupted turn = `error.type=cancelled`
  on the agent span; interrupt rides the events endpoint, not a separate control channel.

## Compaction — user `/compact` on a model-less session (`conv_63542a5f`)
- `POST /events {"type":"compact"}` → **`{"error":{"code":"invalid_input","message":
  "Compaction requires a configured LLM model"}}`** — rejected pre-LLM; session stays idle,
  **no compaction spans emitted**.
- **Confirms #1192:** a session created without an explicit `--model` (subscription default)
  has no `model` on its row, so user-initiated compaction (needs an LLM for the L2 summary)
  fails validation. Auto-compaction in the turn loop is unaffected (it uses the turn's
  resolved model).
- **Beyond code:** `/compact` is gated on a session-row model *before* any summary work —
  an early validation error, not a compaction-loop failure. (A real compaction trace needs a
  model-pinned session + enough context to exceed the threshold.)

## Driving model & runner binding (mechanism finding)
- `omnigent run --server :7777 <agent>` creates a session **with a bound runner** (per-run,
  tunnels to the server) — the reliable SDK driver.
- **REST `POST /v1/sessions {agent_id}` creates a session with NO runner** (`runner_id:null`);
  a later `POST /events` → **`{"error":{"code":"runner_unavailable","message":"No runner bound
  for session"}}`**. The web UI binds one by passing **`host_id`** (+ workspace) at create, so
  the server tells the host to launch a runner.
- **Events envelope** (verified `sessions.py:18282`, `parse_item_data(type, {type, **data})`):
  `POST /events {"type":"message","data":{"role":"user","content":[{"type":"input_text",
  "text":"…"}]}}`. Control events: `{"type":"interrupt"}`, `{"type":"compact"}`.
- **Beyond code:** a session is **inert until a runner is bound**; `host_id` at creation is the
  trigger for the host→runner launch — there is no implicit auto-launch on first event (the
  "runner-offline → stuck working" gap class lives exactly here).

## Sub-agent spawn — debby (claude-sdk orchestrator) → claude + gpt children (`conv_387e2405`)
- The trace exposes the **full spawn mechanism** on `omni-harness`:
  - **`tool:sys_session_send`** (×2) — the spawn call, one per child.
  - children **created** via runner→server `POST /v1/sessions` (×9) — each child is a **full
    session with its own `session.id`** (the trace set touches 3 ids: parent `conv_387e2405`
    + children `conv_9edbfd15`, `conv_3cfe960a`).
  - **`tool:sys_read_inbox`** (×2) — the parent drains child results via the inbox
    (`async_work_complete`) — confirms the consume-once inbox drain.
  - **`GET /v1/sessions/{id}/child_sessions`** (×6) — the subagents-rail/child API.
  - per-child `agent:<name>` spans, each with its **own** `policy.evaluate` gates.
- The gpt child (codex) fails on creds; the claude child answers — debby tolerates partial failure.
- **Beyond code:** confirms §2.F **live** — a sub-agent is a first-class child Conversation
  (own session.id + own runner via the same host), spawned by `sys_session_send` and drained by
  `sys_read_inbox`, **not** an in-process call. Stitching a sub-agent tree across traces requires
  collecting **all** the child `session.id`s (no single trace spans the whole tree).

## claude-native — host-bound session + 1 turn (`conv_7c19d035`); contrast vs SDK turn
- Created via `POST /v1/sessions {agent_id=claude-native-ui, host_id, workspace}` → returns with a
  **live runner** (`runner_online:true`, harness=`claude-native`); message via `POST /events` →
  `{"queued":true,"pending_id":…}` (decoupled async boundary), polled to idle.
- After the turn the session row gains an **`external_session_id`** (the real Claude CLI's session
  UUID, e.g. `c1ffe8d8-…`), persisted via `UPDATE conversations SET … external_session_id=?` — proof
  the vendor CLI ran in tmux and the JSONL forwarder bound to it.
- **The vendor turn is opaque** — the whole turn collapses to a tiny span set rooted by the harness:
  - **`omni-harness agent:claude-native-ui`** [AGENT] carrying `input.value=<user prompt>` and
    **`llm.model_name=claude-native-ui`** (the *harness* name, NOT a real model — the CLI owns model
    selection). **No** child `llm_call`, **no** inline `tool:` spans, **no** per-tool `policy.evaluate`.
  - **`omni-harness claude_native.inject`** — the inject+wait boundary (here 338ms; on a long vendor
    turn this is where all the time hides — a single opaque async span, not a span tree).
  - **`omni-runner claude_native.forward`** (×2) — the JSONL forwarder, each carrying
    `omnigent.response_id=resp_claude_…` (one per vendor message it relays back through runner→server).
- **Gating differs from SDK:** the only `policy.evaluate` is **phase=REQUEST with an empty
  `policy.tool_name`** (gates the inbound *message*), decision=ALLOW. There is **no** `phase=TOOL_CALL`
  per-tool span. (Native tool gating rides the **PreToolUse HTTP hook** out-of-band — it long-polls
  via `_poll_request_disconnect`, sessions.py:1183 — so tool decisions never appear as inline
  `policy.evaluate` spans the way SDK's do; this simple turn used no tools so no hook fired.)
- **Beyond code:** an SDK turn's trace is a deep tree (`agent:` → `llm_call` → `tool:` →
  `POST /mcp` → `policy.evaluate` per tool); a **native** turn's trace is the **opposite shape** —
  one `agent:` + one `inject` + N `forward` spans, with `llm.model_name`=harness-name and a single
  message-level policy gate. The vendor turn is a black box bounded by `claude_native.inject`; you
  observe it only via the forwarder's `resp_claude_*` items, not via internal LLM/tool spans.

## Disconnect / reconnect — SDK turn, SSE dropped mid-flight (`conv_6bbdba9a`)
- Opened `curl -N --max-time 4 …/stream` mid-turn; the live tail delivered, in order:
  `session.heartbeat` → `session.resource.created` (the tmux terminal `terminal_tui_main`, with
  `tmux_socket`/`tmux_target`) → `session.changed_files.invalidated` → `session.presence`, then the
  socket dropped at max-time. **Reopening the stream replays the *identical* opening frames** — i.e.
  the **snapshot-on-connect** (current resource/presence state), **not** the turn's token deltas.
- Code confirms the contract verbatim (sessions.py:16-17 "reconnect contract is **snapshot + live
  tail**, not replay"; 11240-44 "no buffer and no replay … clients reconcile pre-subscribe state via
  the snapshot endpoint and dedupe by item id"). Nuance (11315-20): **in-flight assistant *text*** IS
  re-seeded synchronously at slot registration via `pre_ready_snapshot=inflight_text.snapshot_for(id)`
  (so a mid-turn reconnect re-renders the partial assistant message), while resource state comes from
  the async `on_subscribed` hook — but the *event stream* is never replayed.
- **`[DONE]` sentinel**: emitted as `yield "data: [DONE]\n\n"` in the stream generator's **`finally`**
  (sessions.py:11341; doc 11247) so every clean exit (incl. client disconnect) terminates the SSE
  cleanly. (My `--max-time` kill severed TCP before the flush, so curl never saw it — a graceful
  client close does.) Idle streams stay alive on periodic **`session.heartbeat`** (11262-70).
- **Disconnect detection / teardown**: `_poll_request_disconnect` (sessions.py:1183) blocks on
  `request.receive()` for `http.disconnect`; it's raced (`asyncio.wait`, FIRST_COMPLETED) against the
  verdict Future in the elicitation long-poll **and** the native PreToolUse hook, so a hung-up client
  releases the parked handler immediately instead of waiting out the full timeout.
- **Beyond code:** reconnect = **GET snapshot + subscribe live tail**, deduped by item id — there is
  no server-side replay buffer. The only thing "replayed" is partial assistant *text* (so the cursor
  doesn't blank on refresh); resources/presence are full-state snapshots, deltas are dropped on the
  floor. A turn keeps running through a disconnect (it's runner-side); the client just re-derives view.

## Custom MCP — claude-sdk agent + a stdio echo MCP (`conv_4d0e6cce`)
- Authored a no-dep stdio MCP (`echo_mcp.py`, tool `echo_shout`) wired via agent YAML
  `tools: {echo: {type: mcp, command: …python, args: [echo_mcp.py]}}`; ran with
  `omnigent run echo_agent.yaml --server :7777` (local-runner topology). Agent called it and returned
  `SHOUT: HELLO MCP`.
- Trace: the custom tool's canonical name is **`echo__echo_shout`** — exactly the `{server}__{tool}`
  namespacing (server label `echo` from the YAML key + bare tool `echo_shout`; see
  `mcp_manager.py:139-141`, resolved by prefix-match at `:405-416`). It is gated by **`policy.evaluate`
  phase=TOOL_CALL *and* TOOL_RESULT** (decision=ALLOW) — the same policy pipeline as `sys_*`.
- **Routing is identical at the HTTP layer** but lands in a different executor: omni-server
  `POST /v1/sessions/{id}/mcp` → omni-runner `POST /v1/sessions/{id}/mcp/execute`
  (`http.server_name=runner`). For a **custom** MCP the runner's `RunnerMcpManager` (the
  spec-defined branch in `tool_dispatch.py:10-15`) dispatches into the **stdio subprocess it spawned**;
  for **`sys_*`** the runner routes to server REST/OSEnvironment branches instead (`sys_os_*` →
  runner-local OSEnvironment; `sys_call_async`/file tools → server REST). The `/mcp → /mcp/execute`
  pair is the runner→server→runner proxy hop (`tool_dispatch.py:113-117`).
- **Beyond code:** custom-MCP vs `sys_*` is a **tool-dispatch-ladder** decision inside the runner,
  not a different wire path — both show as `policy.evaluate` → `POST /mcp` → `POST /mcp/execute`. The
  only trace tell is the **tool name**: `{server}__{tool}` (→ RunnerMcpManager stdio child) vs
  `sys_*` (→ server/OS branches). A custom stdio server executes **in the runner process tree**, so
  its blast radius and creds are the runner's, not the server's.

## Timers — `timers: true` claude-sdk agent, `sys_timer_set` (`conv_48ce846b`)
- Agent YAML `timers: true` (gates the tool, `tools/manager.py:168-170`, `:257`); ran
  `omnigent run timer_agent.yaml --server :7777 -p "set a 3s timer…"`. Agent got back
  `timer_id: timer_2ec43…`, then — same run — printed `TIMER_OBSERVED` (the firing landed before the
  runner tore down).
- Trace evidence (full firing loop, **end to end**):
  - **`policy.evaluate tool=sys_timer_set decision=ALLOW phase=TOOL_CALL`** (+ TOOL_RESULT) — the set
    is gated like any tool.
  - the firing re-injects a turn: a `policy.evaluate` whose
    **`policy.content = "[System: timer timer_2ec43… fired]\nnote: 'ping'"`** (REQUEST phase, gating
    the synthetic meta-message), then **`omni-harness agent:timer_agent input.value=[System: timer …
    fired]`** — a brand-new agent turn driven by the timer.
  - net `POST …/events` count jumps (server/runner/harness all show extra `/events`) — the firing is
    literally a server POST, not an in-band callback.
- Mechanism (`tool_dispatch.py:2345-2456`): `sys_timer_set` spawns a runner-side `asyncio` task
  (`_session_timers`, `app.py:7626`) that sleeps then **POSTs `[System: timer {id} fired]` as an
  `is_meta:true` user message** to `/v1/sessions/{id}/events`, re-triggering the agent (`repeat`
  loops; else one-shot). `sys_timer_cancel` cancels the task.
- **Caveat confirmed:** the **runner** path implements timers; the **sessions-native** path raises
  `NotImplementedError` (`tools/builtins/timer.py:14-15,:221`). So timers work under the local-runner
  topology (what I drove) but are a no-op/error on sessions-native.
- **Beyond code:** a timer is **not** a scheduler service — it's a per-session runner-local
  `asyncio.create_task` that, on fire, **re-enters the agent via a synthetic `is_meta` `/events`
  message**. The firing is indistinguishable in the trace from a user turn except the `input.value`
  is `[System: timer … fired]`. Timers die with the runner (no persistence of the task across runner
  restarts — the firing POST is the only durable artifact).

## ASK / DENY policy — session-level `ask_on_os_tools`, resolved DENY (`conv_c8a81cbd`)
- Browsed `GET /v1/policy-registry`, attached the built-in
  **`POST /v1/sessions/{id}/policies {handler:"omnigent.policies.builtins.safety.ask_on_os_tools"}`**
  (`source:session`, enabled) to a host-bound polly (claude-sdk) session; sent a `sys_os_shell`
  trigger.
- The turn **parked on an elicitation** (status stayed `running`). Snapshot
  `pending_elicitations[0]` =
  `{type:"response.elicitation_request", elicitation_id:"elicit_67ff3403…", method:"elicitation/create",
  params:{mode:"url", phase:"tool_call", policy_name:"ask_os_trace",
  message:"ask_os_trace: Agent wants to call sys_os_shell('echo POLICYASKTEST'). Approve?",
  content_preview:"{\"command\": \"echo POLICYASKTEST\"}",
  url:"/approve/conv_…/elicit_67ff3403…"}}`.
- Resolved DENY: **`POST /v1/sessions/{id}/elicitations/{eid}/resolve {"action":"decline"}`** →
  `{"queued":false}` (synchronous; `ElicitationResult.action` ∈ accept/decline/cancel —
  decline = explicit DENY, `schemas.py:1019-1034`). Session returned to **idle**,
  `pending_elicitations:0`, with a `function_call` + `function_call_output` (the **blocked** shell +
  its denial) + a final assistant message — the agent saw the tool refused.
- **First-ever ASK/DENY trace evidence** (we'd only traced ALLOW):
  - **`policy.evaluate tool_name=sys_os_shell decision=ASK phase=TOOL_CALL`** (×2: initial eval + the
    re-eval after the verdict).
  - **`policy.reason = "ask_os_trace: Agent wants to call sys_os_shell('echo POLICYASKTEST'). Approve?"`**
    — the ASK prompt baked into the span (== the elicitation `message`).
  - **`omni-server POST …/elicitations/{eid}/resolve`** span carrying the full
    `…/elicitations/elicit_67ff3403…/resolve` URL — the DENY delivery hop.
- **Beyond code:** the ASK/DENY mechanism is **`policy.evaluate decision=ASK` → publish
  `response.elicitation_request` (mode=url, with a `/approve/…` resolve URL) → park the tool-call
  (turn stays `running`) → client POSTs `ElicitationResult{action}` to the resolve URL → verdict
  unblocks the parked handler**. The resolve URL embeds the unguessable `elicit_…` id as the
  capability (plus `LEVEL_EDIT`). DENY (`decline`) surfaces to the model as a tool *result* (a
  `function_call_output` denial), so the agent continues the turn knowing the action was refused —
  it does **not** error the turn. The same verdict can arrive via the generic `{"type":"approval"}`
  event on `/events`; both route through one `_resolve_elicitation` (sessions.py:3921).
