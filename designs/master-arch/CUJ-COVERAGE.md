# Omnigent CUJ Coverage — every journey in `designs/CUJ-MAP.md`, answered

**Purpose.** One doc that walks **every** journey bullet listed in
[`designs/CUJ-MAP.md`](../../designs/CUJ-MAP.md) §2.A–§2.H and gives each a concrete,
**code + trace-grounded** answer. This supersedes/deepens
[`designs/CUJ-ANALYSIS.md`](../../designs/CUJ-ANALYSIS.md) — the analysis file's `file:line`
anchors had drifted by thousands of lines; the anchors below are **re-derived** from current
`main` (worktree `…/traces`, HEAD `60d11673`) and cross-checked against the per-component
architecture (`architecture/*.md`) and per-domain answer docs (`cuj-answers/*.md`).

**Scope:** claude (sdk + native), codex (sdk + native), Polly / custom agents. Other harnesses
out of scope. **codex / codex-native are live-blocked** (Databricks AI-gateway creds expired,
403 — host reports `codex: needs-auth`) → covered from **code + the §4 capability matrix +
structural analogy** to claude; labelled *(code-only, creds)* throughout.

**Legend:** ✅ works / verified · ⚠️ works-but-caveat or known failure-branch · ❓ not
confirmed this pass. **Trace evidence** cites real span names + conv ids where we drove it live
(rig: local Jaeger `:16686`, persistent `omnigent server :7777` + host; extract via
`scratchpad/trace_tools.py`).

---

## 0. Coverage table (per domain: # journeys → live-traced vs code-only)

| Domain | Journeys | Live-traced | Code-only | Notes |
|---|---:|---:|---:|---|
| **§2.A** Session lifecycle & continuity | 11 | 6 | 5 | fork, switch, interrupt, compaction-reject, send/stream, disconnect traced live |
| **§2.B** Harnesses & per-harness features | 6 | 2 | 4 | claude-native turn + model/runner binding traced; matrix code-verified |
| **§2.C** Tools, MCP, shells, files, timers | 6 | 4 | 2 | `sys_*` (sys_os_shell), custom MCP, timers, shells traced; OmniBox code-only |
| **§2.D** Policies, approvals, elicitations | 7 | 3 | 4 | ASK/DENY, TOOL_CALL gate, REQUEST gate traced live |
| **§2.E** Web UI & clients | 13 | 3 | 10 | web has no OTel spans (opt-in, off) → mostly code; stream/presence/subagent-rail seen via server spans |
| **§2.F** Agents, subagents, executor, routing | 10 | 3 | 7 | sub-agent spawn, executor loop, runner-binding traced; #848/affinity code-verified |
| **§2.G** Onboarding, credentials, auth | 6 | 0 | 6 | creds never surface as spans → all code; PR #1439 verified merged |
| **§2.H** API & message surface | 4 | 2 | 2 | events envelope + SSE families trace-confirmed; full REST catalog code (AST) |
| **Total** | **63** | **~23 (11 distinct live CUJ traces)** | **rest** | 11 distinct live-trace CUJs cover ≥23 journey bullets |

The **11 distinct live traces** (`cuj-answers/_LIVE-TRACE-FINDINGS.md`) are: fork · switch-agent ·
interrupt · compaction(`/compact` reject) · model+runner-binding · sub-agent spawn · claude-native
turn · disconnect/reconnect · custom MCP · timers · ASK/DENY policy. Each is reused across the
domains its journey touches, so one trace often evidences several bullets.

**Deeper docs:** each answer points to `architecture/<component>.md` and/or
`cuj-answers/<domain>.md`. The reliability gaps (§6 of CUJ-ANALYSIS, carried forward + updated) are
consolidated at the **bottom** of this doc.

---

## §2.A  Session lifecycle & continuity

Server logic: `omnigent/server/routes/sessions.py` (912 KB) + `stores/conversation_store/`.
Deeper: `cuj-answers/server-api-state-streaming.md`, `architecture/server.md`.

### ✅ Create a new session (new chat / from existing agent / bundled upload)
`POST /v1/sessions` → `create_session` (`sessions.py:13688`). **JSON** binds an existing `agent_id`;
**multipart** (`metadata`+`bundle`) creates a session-scoped agent + conv row in one txn. Grants
caller `LEVEL_OWNER`, `_announce_session_added` pushes to other tabs. Optional `host_id` triggers
the host→runner launch (+ `workspace` pins the cwd).
**⚠️ inert-until-bound:** REST `POST /v1/sessions {agent_id}` alone returns `runner_id:null`; the
next `POST /events` → `{"error":{"code":"runner_unavailable"}}`. The web binds by passing `host_id`
at create; `omnigent run` binds a per-run runner.
**Trace:** `POST /v1/sessions` root span; the web/host path adds `host.stat`+`host.launch_runner`
CONSUMER spans on `omni-host` (host-channel trace `aee57ff3…`). *(§2.H, §2.G overlap)*
→ `cuj-answers/server-api-state-streaming.md` Q7; `_LIVE-TRACE-FINDINGS.md` "Driving model & runner binding".

### ✅ Resume a session — *how much transcript loads into the runner?*
`GET /v1/sessions/{id}` → `_get_session_snapshot` (`SessionResponse` at `:2458`): metadata +
paginated items + `pending_elicitations` + `pending_inputs` + child sessions; `refresh_state=true`
re-pulls the live runner. **How much loads into the runner:** on the next dispatched turn the runner
calls `GET …/items` and `_load_history_as_input` (`runner/app.py:14842`) rebuilds input —
**full conversation, OR (if a compaction item exists) only the slice after `last_item_id` + the
expanded summary** (`workflow.py:2276` `_load_initial_history`). **SDK** re-drives the executor from
this reconstructed history; **native** keys on `external_session_id` and the vendor CLI reloads its
own store (Omnigent re-injects only if the vendor store is gone). ⚠️ runner offline → snapshot shows
`runner_online:null`.
→ `cuj-answers/executor-subagents-compaction-cache.md` Q6; `runner-dispatch-mcp.md` Q2.

### ✅ Fork a session — *how is the forked transcript constructed?* **[LIVE]**
`POST /v1/sessions/{src}/fork` → `fork_session` (`:15180`, 201). `fork_conversation`
(`sqlalchemy_store.py:2266`) **deep-copies items with fresh ids** (preserving position/response_id),
optional `up_to_response_id` truncation, clones the agent (optional cross-family harness switch →
resets model/effort), **drops instance-scoped labels** (bridge_id, context-token metrics), does
**not** copy `external_session_id`/`workspace`/`git_branch`. Native target rebuilds transcript from
the `FORK_CARRY_HISTORY` label.
**⚠️ can't fork a sub-agent (400, `:15238`).**
**Trace evidence** (`conv_820c6dee` → `conv_7fe12aec`): **server-side only** — new conv returns
`runner_id=null, host_id=null` (no runner spawned for the copy); a burst of **~25 INSERT + many
SELECT on `chat.db`** = the synchronous deep-copy; new session gets a cloned `agent_id` + label
`omnigent.fork.source_id`. **Beyond code:** fork cost is synchronous server DB work that **scales
with item count** (INSERT-per-item) — a latency spike, not a runner concern.
→ `_LIVE-TRACE-FINDINGS.md` "Fork"; `server-api-state-streaming.md` Q7.

### ✅ Switch agent in place (mid-session) **[LIVE]**
`POST /v1/sessions/{id}/switch-agent` → `switch_session_agent` (`:15415`); **idle-only — 409 if
running** (`:15481`); loads target bundle before committing (fail-closed). `switch_conversation_agent`
(`sqlalchemy_store.py:2576`) deletes the old session-scoped agent, clones the target, repoints
`agent_id`, resets model/effort on cross-family, **clears `external_session_id`** (`:2663`) so a
native target cold-starts, stamps a "switch-back" label, publishes `session.agent_changed`.
**Trace evidence** (`conv_820c6dee` → agent `debby`/`ag_3f172efb`): returns immediately, resets
labels, **retains the runner binding** (`runner_token_c2a963…`) — the next turn reuses the same
runner, re-initialized for the new agent (no new runner launch on switch).
→ `_LIVE-TRACE-FINDINGS.md` "Switch-agent"; `harness-behavior.md` Q6.

### ⚠️ Disconnect → reconnect (TUI / WebUI) **[LIVE]** — failure-branch
**Contract = snapshot + live tail, NOT replay.** `_stream_live_events` (`sessions.py:11229`) yields
only from `subscribe` forward — "no buffer, no replay … dedupe by item id" (docstring `:11240`).
Reconnect: open `GET …/stream` first (`ready_event` heartbeat acks the tail slot `:11314`), read
snapshot `GET …/{id}`, dedupe overlap by `ctx.itemId` (web `lib/blockStream.ts`; TUI byte-equal
multiset). `[DONE]` sentinel on **every** exit path (`:11341` `finally`). A turn keeps running through
a disconnect (runner-side); client just re-derives the view.
**Trace evidence** (`conv_6bbdba9a`, SSE dropped mid-flight): reopening the stream **replays the
identical opening frames** (snapshot-on-connect: `session.heartbeat` → `session.resource.created`
[terminal `terminal_tui_main`] → `session.changed_files.invalidated` → `session.presence`), **NOT**
token deltas. **Nuance:** in-flight assistant *text* IS re-seeded synchronously at slot registration
(`pre_ready_snapshot=inflight_text.snapshot_for(id)`, `:11320`) so the cursor doesn't blank; resource
state is full-snapshot, deltas dropped on the floor. Disconnect detection = `_poll_request_disconnect`
(`:1183`) blocking on `request.receive()`, raced against the elicitation/hook futures.
**⚠️ gap: runner-offline-on-message** (below) is the disconnect variant that actually strands a client.
→ `_LIVE-TRACE-FINDINGS.md` "Disconnect / reconnect"; `server-api-state-streaming.md` Q4.

### ✅ Close the page & come back later
Server-durable; the session keeps running while the page is closed. On return, the web
`switchTo(id)` opens `SSE …/stream` first, then `getSessionSlim` + `fetchInitialHistoryWindow`
concurrently, merges deduped by item id (`chatStore.ts:1825-1907`). SSE pump auto-reconnects on
transport drops (Databricks Apps ~5-min HTTP/2 cap); `reconcileOnReconnect` (`:2394`) pages backward
to splice unseen items + recover spinner state. **Host offline →** `ReconnectSessionDialog`
(`ChatPage.tsx:1095`): `host_offline` shows the CLI reconnect command; `local_stranded` shows
`omnigent <run|claude> --resume`. Managed hosts read `host_asleep` (next message wakes the sandbox).
→ `cuj-answers/web-client.md` Q5.

### ✅ Close / archive / delete a session
- **Archive** — `PATCH /v1/sessions/{id} {archived}` (`update_session` `:14795`), **owner-only**
  (`:14823`); plain DB column, hidden from list by default. Pull-based (no WS publish).
- **Close** (sub-agent) — no route; set by `sys_session_close` → label `omnigent.closed=="true"`
  OR legacy title `:closed:` (`is_session_closed`, `session_lifecycle.py:70`). `post_event` rejects
  new user input (409, `:18306`); **reads still allowed**.
- **Delete** — `DELETE /v1/sessions/{id}` (`:19358`), **owner-only** (admins bypass); best-effort
  runner-resource cleanup, file/artifact delete, optional `?delete_branch` worktree removal, managed-
  host teardown. ⚠️ runner offline → orphans runner resources (falls back to local registry cleanup).
→ `cuj-answers/server-api-state-streaming.md` Q7.

### ✅ Send a message + receive a streaming response **[LIVE]**
`POST /v1/sessions/{id}/events` → `post_event` (`:18150`, returns 202). **Invariant I1:
persist-before-forward** — `conversation_store.append([item])` first (`:8540`), then POST to runner
(`:8697`), then `_publish_input_consumed` carrying the persisted `item_id` (`:8704`) for client
dedup. Streaming deltas = `response.output_text.delta`; final text flushed to a durable `message` at
a function-call boundary or terminal `response.*`.
**Events envelope** (verified `:18282`, `parse_item_data(type, {type,**data})`):
`{"type":"message","data":{"role":"user","content":[{"type":"input_text","text":"…"}]}}`.
**Trace evidence** (tool-use conv, 415 spans): `POST /events` → `policy.evaluate` → `UPDATE`+`INSERT
chat.db` (the append) → cross-edge `POST` to `omni-runner /events`; the append precedes the runner POST
(**I1 visible on the wire**); runner calls back `GET /items` + `GET /sessions/{id}`.
**⚠️ deny path:** policy deny → persist sentinel, terminal `response.completed` (headless `-p`
unblock), status→idle, **not forwarded**. **⚠️ runner offline:** fresh item with no runner+no host →
503 `RUNNER_UNAVAILABLE` **before** persist (`:19073`); if a runner was bound but offline → persisted,
forward fails, idle published, client bubble may sit until snapshot reconciliation.
→ `cuj-answers/server-api-state-streaming.md` Q1; `_LIVE-TRACE-FINDINGS.md` "Driving model".

### ⚠️ Compaction / context-window overflow **[LIVE]** — failure-branch
3-layer, least→most lossy (`runtime/compaction.py:544`): **L1** surgical clear of tool-result bodies
→ **L2** LLM summary (routed through runner `POST /v1/summarize` for the runner's creds, `:428`) →
**L3** emergency truncate. Budget = `context_window*0.8 − system_budget`.
**Two triggers (the "runner catches overflow → compacts" belief is only half right):**
- **Proactive/threshold (the real auto-compaction):** in the in-process loop
  `_call_llm_maybe_compact` (`workflow.py:2057`) at 0.8 threshold.
- **⚠️ Reactive on harness-reported overflow does NOT auto-compact:** `_is_context_overflow_error`
  (`runner/app.py:6736`) → `_ContextWindowOverflow` (`:14243`) → caught in `_run_turn_bg` (`:13804`)
  which **ends the turn with an error** (resume-overflow surface, OMNI-143).
- **Explicit `/compact`:** `compact_conversation_now` (`workflow.py:2347`, `force=True`).
**Trace evidence** (`conv_63542a5f`, `/compact` on a **model-less** session): `POST /events
{"type":"compact"}` → `{"error":{"code":"invalid_input","message":"Compaction requires a configured
LLM model"}}` — **rejected pre-LLM, no compaction spans**. **Confirms #1192:** a session created
without `--model` (subscription default) has no `model` on its row, so user `/compact` (needs an LLM
for L2) fails validation; auto-compaction in the loop is unaffected (uses the turn's resolved model).
[memory: compact-every-msg fixed #1082 — the broken-cursor guards in `_load_initial_history` are that
fix. ⚠️ resume-overflow OMNI-143 still open.]
→ `cuj-answers/executor-subagents-compaction-cache.md` Q5; `_LIVE-TRACE-FINDINGS.md` "Compaction".

### ⚠️ First-message delivery / optimistic pending input — failure-branch
`runtime/pending_inputs.py`; the client paints a `PendingUserMessage{tempId}` **before** the POST
(`chatStore.ts:791`) so a `session.input.consumed` racing ahead still finds it; on consumed the bubble
is promoted to a committed block by `clearedPendingId` → FIFO head → payload. Snapshot includes
`pending_inputs` on reconnect. **⚠️ native FIFO-desync class:** native web messages get no server
`item_id` at dispatch (only a `pending_id`); dedup vs the mirrored-back transcript relies on client
FIFO/stableKey + `cleared_pending_id` — the double-bubble risk lives exactly here
(`server-api-state-streaming.md` Q2).
→ `cuj-answers/web-client.md` Q3.

### ⚠️ Local↔server transcript reconstruction & mismatch — invariant
Server conversation history is the source of truth. **SDK:** 100% Omnigent; the client reconciles
streaming↔durable by `ctx.itemId` (web) / byte-equal multiset (TUI). **Native:** the **vendor store
is the source of truth**, mirrored back — mismatch risk is highest here (native FIFO-desync, above;
plus reasoning is persisted on native but recomputed on SDK). Post-fork = fresh-id deep copy;
post-resume = compaction-bounded slice; post-compaction = summary pair + items after `last_item_id`.
**❓ open probe (CUJ-MAP §5):** local↔server mismatch cases *beyond* compaction/fork need a dedicated
test (multi-replica in-memory status/pending caches, `server-api-state-streaming.md` Q3/OpenQ).
→ `cuj-answers/tui-vs-web.md` Q1; `server-api-state-streaming.md` Q6.

---

## §2.B  Harnesses & per-harness features

Taxonomy: **SDK** (in-process loop, Omnigent owns prompt+tools+transcript; base `inner/executor.py`)
vs **Native** (resident vendor CLI in tmux/socket, vendor owns prompt+tools, transcript mirrored;
base `native_server_harness.py`). Deeper: `cuj-answers/harness-behavior.md`, `architecture/harness-inner.md`.

### ✅ Pick a harness at session start
`omnigent <harness>` or `omnigent run --harness X`. Aliases `harness_aliases.py:9`
(`claude`→`claude-sdk`); validate at `cli.py:5554`. **⚠️ native + AGENT-spec combo rejected**
(`cli.py:5874`). Web: harness selector in NewChatDialog (localStorage per agent,
`lib/modePreferences.ts`). Host gates the launch on `harness_is_configured(frame.harness)`
(`host/connect.py:766`) → `HARNESS_NOT_CONFIGURED` (412) if the CLI/cred is missing.
→ `cuj-answers/host-channel.md` (per-harness); `harness-behavior.md` §2.

### ✅ Switch harness mid-session
Via `switch-agent` (idle-only) or `fork` (optional harness switch). For **native** targets it clears
`external_session_id` so the next turn rebuilds the vendor transcript; cross-family switches reset the
model. claude-native/codex-native key their bridge to a `bridge_id` from session labels so
`--resume`/fork land in the right pane/thread.
→ `cuj-answers/harness-behavior.md` Q6.

### ✅ Change model / effort — at start and mid-session (from WebUI) **[partial LIVE via native turn]**
- **claude-sdk** — next-turn config: `cfg.model` read each turn; `client.set_model()` if changed
  (`:1422/1910`); effort via `extra["reasoning_effort"]`→SDK `effort` (`:2011`). Applies **next** turn.
- **codex (SDK)** — model is part of the app-session **signature**; changing it **closes the
  app-session and re-threads** (`:2346/2301`); effort resets on the new thread (`:1450`).
- **⚠️ claude-native** — best-effort, two writers: web `/model` → runner types `/model X` into tmux
  (`app.py:11558`, `auto_confirm`); the `--model` flag is baked at spawn (not re-read at turn
  boundaries); the forwarder mirrors an in-pane `/model` back to `model_override` every poll
  (`:948`). **⚠️ `/effort none|minimal` are persist-only** — skipped because not in CLAUDE_EFFORTS
  (`:11521`). Model change **never affects the running turn** (next turn only).
- **codex-native** — RPC: `thread/settings/update` (`app.py:10664`, exec `:237/266`); the TUI's own
  model/effort mirrored into the snapshot.
Effort source of truth = `reasoning_effort.py` (`CLAUDE={low,med,high,xhigh,max}`;
`OPENAI/CODEX={none,minimal,low,med,high,xhigh}`). Selectable at start (NewChatDialog) + `/effort`.
**Trace:** claude-native turn (`conv_7c19d035`) shows `llm.model_name=claude-native-ui` = the *harness*
name, **not** a real model (the CLI owns model selection).
→ `cuj-answers/harness-behavior.md` §3; **⚠️ #1128** below.

### ⚠️ Default model / provider resolution — carries **#1128**
Chain (highest first): CLI `--model` → `OMNIGENT_MODEL` env → YAML `executor.model` →
`~/.omnigent/config.yaml` default → per-harness fallback (`chat.py`). Ad-hoc specs →
`_DEFAULT_AD_HOC_MODEL="databricks-gpt-5-4"` (`chat.py:99`).
**⚠️ #1128 (billing) — VERIFIED at `claude_sdk_executor.py:1910-1912`:**
`model = cfg.model or self._model_override`; if still None →
`model = _DATABRICKS_CLAUDE_DEFAULT_MODEL` (= `databricks-claude-opus-4-8`, `databricks_config.py:85`)
**only on the Databricks-profile gateway path**. Fires when Sonnet was intended but the override
arrived None → silently bills Opus. Real fix PR #1146 (unmerged); frontend PRs #1570/#1563 do **not**
fix it. codex has the analogous fallback at `:2334`.
→ `cuj-answers/harness-behavior.md` §4; gaps §2.B below.

### ⚠️ Propagate the user's OWN harness config into omni (`~/.claude`) (#3)
- **claude-native** — `use_claude_config` flag (`claude_native.py:349`): default `False` = omni-managed
  isolated HOME + MCP relay (`:400`); `True` **passes through the user's `~/.claude/` credentials,
  settings.json, MCP servers, hooks** (`:371`). Strongest passthrough. ⚠️ user `settings.json` model
  can conflict with omni `--model`.
- **codex-native** — inherits `~/.codex/config.toml` + `auth.json` as baseline via the CODEX_HOME
  source resolver (`codex_native.py:131`); omni `--model`/effort layer on top via RPC.
- **SDK harnesses** — config via `HARNESS_*` env vars from the workflow; they do **not** pass through
  CLI dotfiles (claude-sdk explicitly strips `ANTHROPIC_API_KEY` to preserve subscription auth,
  `claude_sdk_harness.py:120`).
→ `cuj-answers/harness-behavior.md` §5.

### ✅ Native vs SDK behavioral differences **[LIVE — both shapes]**
| Concern | SDK (claude-sdk, codex) | Native (claude-native, codex-native) |
|---|---|---|
| Agent loop | in-process `run_turn` drives vendor SDK | resident vendor CLI (tmux / JSON-RPC socket) |
| System prompt / tools | **Omnigent** | **Vendor** (omni's are `del`'d, `claude_native_executor.py:123`) |
| Transcript | Omnigent owns 100% | vendor store is truth, mirrored |
| Turn output | streamed `ExecutorEvent`s | forwarder polls store → `external_*` events |
**Trace contrast (the headline finding):** an **SDK** turn = a **deep tree** (`agent:claude-sdk` →
`tool:sys_os_shell` → `POST /mcp` → `policy.evaluate` per tool). A **native** turn (`conv_7c19d035`)
= the **opposite shape**: one `agent:claude-native-ui` (input.value + `llm.model_name`=harness-name,
**no** child `llm_call`, **no** inline `tool:` spans) + one `claude_native.inject` (the inject+wait
boundary where all vendor time hides) + N `omni-runner claude_native.forward` (the JSONL forwarder,
each carrying `resp_claude_*`). The vendor turn is a **black box**; you observe it only via the
forwarder's items. Native session row gains `external_session_id` (the real CLI's UUID) after the turn.
→ `cuj-answers/harness-behavior.md` §2; `_LIVE-TRACE-FINDINGS.md` "claude-native".

**Per-harness support matrix** (code-verified against each `inner/*_executor.py`; base defaults all ❌
except `supports_tool_calling`, `executor.py:541-585`):

| Harness | interrupt | queue | subagents | reasoning effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|
| **claude-sdk** | ✅ `client.interrupt()` (`:1477`) | ✅ (`:1614`) | ✅ `sys_session_*` | ✅ {low..max} | ✅ `can_use_tool` bridge | ✅ next turn |
| **codex** *(code-only)* | ✅ `turn/interrupt` (`:2243`) | ✅ (`:2240`) | ⚠️† `CODEX_HOME` isolation | ✅ {none..xhigh} | ❌ `approvalPolicy:"never"` (`:1427`) | ⚠️ re-threads (`:2346`) |
| **claude-native** | ✅ bridge `Escape` (`bridge:2530`) | ✅ | ✅ `external_subagent_start` (`fwd:1115`) | ✅ `/effort` (⚠️ none/minimal skipped) | ✅ PreToolUse+PermissionRequest long-poll | ✅ `/model` inject; next turn |
| **codex-native** *(code-only)* | ✅ `turn/interrupt` (`exec:116`) | ✅ `turn/steer` | ✅ `external_codex_subagent_start` (`fwd:6079`) | ✅ `thread/settings/update` | ✅ `codex-elicitation-request` long-poll | ✅ RPC; next turn |

claude-sdk uniquely overrides `supports_tool_boundary_interrupt`→✅ (`:1617`). **Polly** has no row —
its brain runs on claude-sdk (reads as that row); its workers are claude_code (claude-native) / codex
(codex-native). † codex-SDK subagents = process isolation (private CODEX_HOME), not a declared mirror;
codex-*native* subagents are real (`external_codex_subagent_start`).
→ `cuj-answers/harness-behavior.md` §1; `CUJ-ANALYSIS.md §4`.

---

## §2.C  Tools, MCP, shells, files, timers

Deeper: `cuj-answers/tools-mcp-shells.md`, `runner-dispatch-mcp.md`, `architecture/tools-omnibox.md`.

### ✅ Use the Omnigent MCP (`sys_*` tools) (#6) **[LIVE]**
All builtins register in `tools/manager.py` (`ToolManager.__init__:105`). Groups + gates: File/shell
`sys_os_read/write/edit/shell` (os_env set, `:525`); Terminals `sys_terminal_*` (non-empty terminals,
`:563`); Async/inbox `sys_call_async`/`sys_read_inbox`/`sys_cancel_async` (`async_enabled` default
True, `:200`); Timers `sys_timer_set/cancel` (`timers:true`, `:231`); Sub-agents
`sys_session_send/create/close/list/get_history/get_info/share` (`:427-468`); Agents `sys_agent_*`
(`:471`); Policy `sys_add_policy`/`sys_policy_registry` (always, `:186`); Comments
`list_comments`/`update_comment` (`:511`). Opt-in flags control **advertisement, not authority**.
**Trace evidence** (`conv_63542a5f`, `sys_os_shell echo TRACETEST123`): OpenInference tree
`omni-harness agent:claude-sdk` → `tool:sys_os_shell` (kind=TOOL); the MCP+policy chain =
`policy.evaluate REQUEST/LLM_REQUEST` → `POST /mcp` → `policy.evaluate TOOL_CALL` (the gate) →
`POST /mcp/execute` → `policy.evaluate TOOL_RESULT` → `LLM_RESPONSE`, all ALLOW.
→ `cuj-answers/tools-mcp-shells.md` Q1.

### ✅ Register & use a custom (user-defined) MCP server **[LIVE]**
Declared in YAML `tools.mcp` → `MCPServerConfig` (`spec/types.py:~847`): `transport:"http"` (SSE) or
`"stdio"` (subprocess); per-server tool allowlist + timeout/retry. Pooled by `RunnerMcpManager`
(`runner/mcp_manager.py`): lazy connect, **8-entry LRU** keyed by spec hash; tools namespaced
`{server}__{tool}` (`:139`). A custom MCP can request inline `elicitation/create` → web card
(`:182`).
**Trace evidence** (`conv_4d0e6cce`, stdio echo MCP): canonical tool name **`echo__echo_shout`** (the
`{server}__{tool}` namespacing); gated by `policy.evaluate TOOL_CALL + TOOL_RESULT` (same pipeline as
`sys_*`). **Routing is identical at the HTTP layer** (`POST /mcp` → `POST /mcp/execute`) but lands in
a different executor: a **custom** MCP → the runner's `RunnerMcpManager` dispatches into the **stdio
subprocess it spawned** (so its blast radius/creds are the runner's); `sys_*` → server REST /
runner-local OSEnvironment. The only trace tell is the tool name.
→ `cuj-answers/tools-mcp-shells.md` Q2; `_LIVE-TRACE-FINDINGS.md` "Custom MCP".

### ✅ MCP routing — who routes a tool call where? **[LIVE]**
**Two managers, chosen by mode** (`proxy_mcp_manager.py:15`): **Deployed** = `ProxyMcpManager`
(server enforces policy at `/mcp`, delegates execution to runner); **No-server/test** =
`RunnerMcpManager` (runner enforces via `RunnerToolPolicyGate`). The deployed loop: harness tool call
→ runner POSTs `POST /v1/sessions/{id}/mcp` → server runs TOOL_CALL policy → **calls back**
`POST /v1/sessions/{id}/mcp/execute` (`app.py:17829`); the **`__` separator is the routing key**
(`:17954`): `"__" in name` → custom/user MCP → `RunnerMcpManager`; no `__` → runner-local builtin →
`execute_tool`. Split is deliberate: **server owns policy+routing; runner owns execution** (right
machine/cwd/env). **Native:** `sys_*` ride the in-turn relay (server-checked) so the PreToolUse hook
**skips `mcp__omnigent__*`** to avoid double-eval (`native_policy_hook.py:209`); connector/custom MCP
still hits the hook.
→ `cuj-answers/runner-dispatch-mcp.md` Q3.

### ✅ Use shells (#4) — *cwd? how exposed to agents?* **[LIVE]**
Two paths: **`sys_os_shell`** (one-shot in the agent's shared `OSEnvironment`, `os_env.py:294` — all
`sys_os_*` share one instance) and **`sys_terminal_*`** (persistent named tmux panes,
`terminals/registry.py:160`, `remain-on-exit on`, surviving turns). **cwd precedence**
(`_resolve_cwd`, `sys_terminal.py:752`, first match): LLM `cwd_override` → `terminal.os_env.cwd` →
`spec.os_env.cwd` → `ctx.workspace` → *(implicit)* host/runner cwd. Orphan tmux servers reaped at
runner startup (`terminal.py:581`); native panes reaped after 30-min idle (`pane_reaper.py`, #1349).
**Trace evidence:** the `sys_os_shell` TOOL_RESULT carried `cwd=/Users/.../traces` (the runner
workspace) → confirms cwd tier 4/5.
→ `cuj-answers/tools-mcp-shells.md` Q4.

### ⚠️ OmniBox / OS sandbox (filesystem + network isolation + credential injection) *(code-only)*
**OmniBox = the OS-level sandbox**, not a web component. `OSEnvironment` modes: `caller_process`
(none) · `fork` (workspace copy) · `sandbox`. Backends (`inner/sandbox.py:371`): `linux_bwrap`
(namespaces + seccomp denylist + tmpfs-masked dotfiles), `darwin_seatbelt` (`sandbox-exec` SBPL,
deny-default; **no namespaces/seccomp**), `windows_jobobject` (process containment only; **no
FS/network isolation**). **Three layers:** (1) filesystem isolation (only granted paths visible), (2)
**default-deny L7 egress proxy** (`inner/egress/`: DSL rules, DNS-safe host allowlist, private-IP +
cloud-metadata block via resolve-once, MITM with per-sandbox CA), (3) **credential injection**
(swap-on-access default — nothing credential-shaped in sandbox, proxy injects the real secret for the
bound host; leak-guard 403 on wrong host). Real secret lives **only** in parent + proxy table, never
in `SandboxPolicy`/argv/disk.
**⚠️ #1542 (SECURITY):** `credential_proxy.py:190` runs parent-side `subprocess.run(shell=True)` +
arbitrary file reads on an unenforced "trusted-spec-only" assumption. **⚠️ #517:** sandboxed
claude-sdk **crashes on macOS** instead of degrading (part-1 no PR).
→ `cuj-answers/tools-mcp-shells.md` Q5.

### ⚠️ Timers & async background work **[LIVE]**
`sys_timer_set` returns a `timer_id` synchronously; the firing arrives as `[System: timer X fired]`
via the `async_work_complete` drain. **Mechanism** (`tool_dispatch.py:2345`): a runner-side
`asyncio` task (`_session_timers`, `app.py:7626`) sleeps then **POSTs `[System: timer {id} fired]` as
an `is_meta:true` user message** to `/events`, re-triggering the agent.
**Trace evidence** (`conv_48ce846b`, `timers:true`, 3s timer): full firing loop end-to-end —
`policy.evaluate tool=sys_timer_set ALLOW TOOL_CALL` (set is gated), then the firing re-injects a turn:
`policy.evaluate content="[System: timer … fired]" REQUEST` → `agent:timer_agent input.value=[System:
timer … fired]` (a brand-new agent turn). Net `/events` count jumps — the firing is literally a server
POST, not an in-band callback. **⚠️ caveat CONFIRMED:** the **runner** path implements timers; the
**sessions-native** path raises `NotImplementedError` (`timer.py:220`). Timers die with the runner (no
persistence). Async: `sys_call_async` fire-and-forget → results drain via `async_work_complete`
(auto each iteration OR `sys_read_inbox`, consume-once). **⚠️ `sys_cancel_task`/`sys_cancel_async` =
NO-OP** — tasks table removed → `task_not_found` for every input (`async_inbox.py:108`).
→ `cuj-answers/tools-mcp-shells.md` Q6; `executor-subagents-compaction-cache.md` Q4; `_LIVE-TRACE-FINDINGS.md` "Timers".

---

## §2.D  Policies, approvals, elicitations

One engine (`runtime/policies/engine.py:43`), two surfaces (server + runner fast-path). Deeper:
`cuj-answers/policy-enforcement.md`, `architecture/policies.md`.

### ✅ Create / add a policy (session / admin-default / spec) (#2) **[partial LIVE]**
Three sources merged at engine-build (`builder.py:309`, order session→spec→admin + hardcoded gate):
- **Session** — `sys_add_policy` (after browsing `sys_policy_registry`) → `POST
  /v1/sessions/{id}/policies` (`session_policies.py:148`). Handler **must be in the registry
  allowlist** (anti-RCE, `:181`); activates immediately. **Hardcoded guard:** `ask_on_add_policy` is
  unconditionally appended to every engine (`builder.py:315`) so an agent can never add a policy
  without a human ASK.
- **Admin default** — `POST /v1/policies` (`default_policies.py:129`, `_require_admin`); `session_id
  IS NULL`; applies server-wide (appended last → "admin gets the last word").
- **Spec-declared** — YAML `guardrails.policies:` → `source="spec"`, `id=None`, immutable.
**Trace evidence** (`conv_c8a81cbd`): browsed `GET /v1/policy-registry`, attached built-in
`ask_on_os_tools` via `POST …/policies {handler:"…safety.ask_on_os_tools"}` (`source:session`).
→ `cuj-answers/policy-enforcement.md` Q1; `_LIVE-TRACE-FINDINGS.md` "ASK / DENY policy".

### ✅ Update / enable-disable / remove a policy (#2)
`PATCH /v1/sessions/{id}/policies/{pid}` (`:275`) / `PATCH /v1/policies/{pid}` (`:234`): mutate
`name`/`handler`/`enabled` (the disable toggle); `type` immutable; PATCH re-checks the registry
allowlist for handler changes (no back door, `:323`). `DELETE …/{pid}` idempotent. Spec policies
can't PATCH/DELETE.
→ `cuj-answers/policy-enforcement.md` Q1.

### ✅ Get denied / get approved — the ASK flow (#2) **[LIVE]**
Engine composes ASK with **withheld** `set_labels`+`state_updates`. Parks two ways: **server-parked**
(REQUEST gate / native TOOL_CALL via hook / LLM_REQUEST) via `_hold_native_ask_gate` (`:4119`,
publishes `response.elicitation_request` mode=url); **runner-parked** (SDK relay TOOL_CALL) returns
`verdict:"pending"`+`elicitation_id`, runner parks on `_pending_approvals`. Resolve = `POST
…/elicitations/{eid}/resolve` (`:18022`) OR `{type:"approval"}` event → both funnel into
`_resolve_elicitation` (`:3921`): set Future → publish `response.elicitation_resolved` (badge −1) →
forward to runner. **APPROVE** (`action=="accept"`, strict) applies withheld writes; **DENY/cancel/
timeout/disconnect** discards them (no trace).
**Trace evidence (first-ever ASK/DENY trace)** (`conv_c8a81cbd`, `ask_on_os_tools` → DENY): the turn
**parked** (status stayed `running`); `pending_elicitations[0]` = `{mode:"url", phase:"tool_call",
message:"ask_os_trace: Agent wants to call sys_os_shell('echo POLICYASKTEST'). Approve?",
url:"/approve/conv_…/elicit_67ff3403…"}`. Resolved DENY via `POST …/elicitations/{eid}/resolve
{"action":"decline"}` → session returned to **idle**, `pending_elicitations:0`, with a
`function_call`+`function_call_output` (the **blocked** shell + its denial) + a final assistant
message. Spans: `policy.evaluate tool=sys_os_shell decision=ASK TOOL_CALL` (×2: initial + re-eval
after verdict); `policy.reason` == the elicitation message; `omni-server POST …/elicitations/{eid}/
resolve`. **DENY (`decline`) surfaces to the model as a tool *result*, not a turn error** — the agent
continues knowing the action was refused.
→ `cuj-answers/policy-enforcement.md` Q4; `_LIVE-TRACE-FINDINGS.md` "ASK / DENY policy".

### ✅ Enforcement: server-level vs session/runner-level **[LIVE]**
**Server** (default+spec+admin): REQUEST/RESPONSE gates at `POST /events`; TOOL_CALL/TOOL_RESULT via
the `/mcp` proxy (`_handle_mcp_tools_call`); LLM-phase + the elicitation registry via `POST
/policies/evaluate`. **Runner fast-path** (`RunnerToolPolicyGate`, `runner/policy.py:109`): runs only
function-type TOOL_CALL/TOOL_RESULT policies, decides ALLOW/DENY **locally before MCP dispatch**; ASK
escalates to the server (dual evaluation is intentional). **Trace reality:** the observed claude-sdk
run routed TOOL_CALL/TOOL_RESULT through the **server `/mcp` proxy** (because `sys_os_shell` is an
Omnigent tool dispatched server-side); the runner fast-path fires for function-type spec policies
bound to tools + connector-native tools.
→ `cuj-answers/policy-enforcement.md` Q2.

### ✅ What types of hooks capture elicitations / questions (vs policy hooks)?
**Phases (5):** REQUEST (input gate) · TOOL_CALL (main gate) · TOOL_RESULT (post, DENY/redact only) ·
advisory LLM_REQUEST/LLM_RESPONSE. **Elicitation/question hooks** (native): PreToolUse (→ TOOL_CALL),
UserPromptSubmit (→ REQUEST), PermissionRequest (`/hooks/permission-request`), and vendor-specific
`codex-elicitation-request` / `ask-user-question`. **Composition:** session→spec→admin; **first DENY
short-circuits**; ASK accumulates (later DENY overrides); ALLOW chains `data` forward. **Fail-closed
vs open:** `FAIL_CLOSED_PHASES = (TOOL_CALL, REQUEST)` (`types.py:61`) → DENY on outage; TOOL_RESULT/
LLM_* → fail-OPEN. **Read-only eval (LEVEL_READ):** policies run but side-effects not persisted
(audit "what would be denied"); read-only callers never enter the ASK gate.
→ `cuj-answers/policy-enforcement.md` Q3, Q6.

### ✅ Which hooks must a harness expose for ALL policies to work?
| Harness | Required hooks | Verdict delivery |
|---|---|---|
| **claude-native** | PreToolUse (TOOL_CALL) + **UserPromptSubmit** (REQUEST — the sole native REQUEST gate) + PermissionRequest | **long-poll HTTP** (verdict in held body); server collapses policy-ASK→hard ALLOW/DENY so permissive `permission_mode` can't auto-approve |
| **codex-native** *(code-only)* | codex PreToolUse/UserPromptSubmit-equiv (shared `native_policy_hook`) + `codex-elicitation-request` | long-poll HTTP |
| **claude-sdk / codex / Polly** | server `type=approval` event (no command hooks) | runner `pending_approvals` Future / SDK `can_use_tool` callback |
Shared translation layer `native_policy_hook.py` converts hook shape ↔ proto. **No keystroke
emulation for any in-scope harness.**
→ `cuj-answers/policy-enforcement.md` Q5; `harness-behavior.md` Q7.

### ✅ How does an elicitation response get back to the harness? (keystrokes? something better?) — carries policy-token gap
For in-scope harnesses: **long-poll HTTP hold** (claude-native/codex-native — verdict in the held
response body) or an **`approval` event → Future** (SDK). **Not keystrokes.** (Some out-of-scope
native harnesses use tmux keystrokes — not relevant here.)
**⚠️ policy-token failure-branch — VERIFIED FIXED for in-scope native (PR #1439, merged commit
`e9561916`):** the native PreToolUse/PermissionRequest hooks launch with a **one-shot bearer**
snapshotted at launch (`claude_native_hook.py` reads it; wrapper `native_policy_hook.py`). **Old
bug:** the Databricks Apps front door bounced the ~1h-expired bearer with a **302→/oidc/** (not a
401), the hook couldn't get a verdict, and TOOL_CALL (a fail-closed phase) **failed CLOSED** — even
though chat kept working. **Fix (PR #1439):** on a 302→/oidc|/.auth **or** 401 the hook **re-mints a
fresh bearer** via `policy_hook_reauth` (`claude_native_hook.py:36-37,525-580`; wired `:657,:729`)
and **retries once** (`post_evaluate_with_retry`, branch reached `:875`); fail-closed remains the last
resort when no token can be minted (preserves #163/#579). **Residual (out of scope):** the OpenCode
policy plugin snapshot (`runner/app.py:1141-1149`, `OMNIGENT_POLICY_AUTH`) still uses a one-shot token
and **degrades fail-OPEN** after ~1h (comment: "a refreshable token file is the follow-up").
→ `cuj-answers/credentials.md` Q4; `policy-enforcement.md` Q5; gaps §2.G below.

---

## §2.E  Web UI & clients

React app under `web/src/` (renamed from `ap-web/` upstream); TUI/REPL under `omnigent/repl/`.
**No `omni-web` OTel spans** (opt-in `VITE_OTEL_EXPORTER_OTLP_ENDPOINT`, inactive) → code-grounded;
server-side spans corroborate the wire. Deeper: `cuj-answers/web-client.md`, `tui-vs-web.md`,
`architecture/web.md`, `tui-repl.md`.

### ✅ Sidebar: browse / search sessions
`useConversations` (`hooks/useConversations.ts:216`): `useInfiniteQuery` over `GET /v1/sessions`
(`order=desc`, `sort_by=updated_at`, `limit=20`, `search_query=` debounced ~300ms, cursor
`getNextPageParam=last_id`). Live via **`WS /v1/sessions/updates`** (`sessionUpdatesSocket.ts`):
client sends a **watch-set**; server pushes `snapshot`/`changed`/`removed`/`heartbeat`; 70s watchdog
→ reconnect. HTTP fallback: 60s connected / 45s disconnected.
→ `cuj-answers/web-client.md` Q1.

### ✅ Organize sessions into projects (#7)
`useProjects` → `GET /v1/sessions/projects`; **implicit** (exist iff ≥1 non-archived session); stored
as reserved label `omni_project`; collapsible (localStorage); lazy `GET /sessions?project=`. Set at
start (NewChatDialog) or kebab → Change project.
→ `cuj-answers/web-client.md` Q1.

### ✅ Pin / unpin (#7); archive / rename / delete
Pin = localStorage `omnigent:pinned-conversation-ids` (drag-reorder; off-window pins backfilled via
per-id GET). Grouping precedence **Archived > Pinned > Project > Recent**. Archive/rename/delete =
`PATCH archived` / `PATCH title` / `DELETE` (`useConversations.ts:250,267,285`).
→ `cuj-answers/web-client.md` Q1.

### ✅ Check the inbox — approvals + unseen comments (#8)
`pages/InboxPage.tsx` (`/inbox`): pending approvals (drains all session pages, filters
`pending_elicitations_count>0`) **+** unseen file comments (`useCommentInbox`); comment clears when
viewed. Badge = `pending_elicitations` (auto-tracked by `record_publish` on every
`response.elicitation_request`, `pending_elicitations.py:81`) + comment fingerprint.
→ `cuj-answers/web-client.md`; `policy-enforcement.md` Q6.

### ✅ Comment on files & send comments to the agent (#9)
`shell/CommentsPanel.tsx`, `FileViewer.tsx`, `hooks/useComments.ts`, Monaco gutter decorations.
Select text → comment (char offsets, `POST …/comments`); open vs addressed tabs; **"Address All"** →
`POST …/comments/send` (`useComments.ts:164`) posts comments to the agent. Authz: read=viewer,
create=editor, edit/delete=author|owner.
→ `cuj-answers/web-client.md` Q2.

### ✅ Share a session / collaborate (#1)
`ChatHeader.tsx` Share + `PermissionsModal.tsx` + `usePermissions.ts`. Levels **0/1/2/3 =
none/view/edit/manage** (`PUT …/permissions`, incl. `__public__` toggle); copy share link `/c/:id`;
requires manage(3). Live **presence avatars** (`PresenceAvatars.tsx`) — holding `…/stream` open
registers a viewer (`presence.connect`, `_stream_live_events:11307`), tree-scoped so sub-agent viewers
see each other.
**Trace corroboration:** the disconnect trace (`conv_6bbdba9a`) shows `session.presence` in the
snapshot-on-connect frames.
→ `cuj-answers/web-client.md` Q2; `server-api-state-streaming.md` Q3.

### ✅ Members admin (invite / reset password / delete user)
`pages/MembersPage.tsx` (`/members`, admin, accounts mode): `GET /auth/users`, `POST /auth/invite`
(single-use URL shown once), `POST /auth/users/{id}/reset`, `DELETE /auth/users/{id}` (cascades).
Note: `GET /v1/users/search` is **not** a direct SPA call — it's a host-injected callback
(`useUserSearch.ts:25`), inert in standalone.
→ `cuj-answers/web-client.md` Q2.

### ✅ See "working vs idle" state — and how it propagates
**Single server funnel** `_publish_status` (`sessions.py:5343`): writes `_session_status_cache` (the
sidebar bridge) **and** publishes a `session.status` SSE event. Status edges: SDK = runner
`session.status` → relay → `_publish_status`; native = `external_session_status` → `_publish_status`.
**Stickiness:** a cached `failed` is not overwritten by a trailing `idle` (`:5389`). **Client:**
sidebar badge (awaiting>running>none) from WS frames; chat "Working…" (idle/launching/running/waiting/
failed) from SSE `session.status`; `background_task_count` sticky. **⚠️ multi-replica:**
`_session_status_cache` is per-process (`:837`) — a replica may read a status another replica's relay
wrote (open question).
→ `cuj-answers/server-api-state-streaming.md` Q3; `web-client.md` Q4.

### ✅ Reconcile streaming vs durable messages into one coherent view
The renderer walks one flat `blocks[]` from (a) durable snapshot items (each with `ctx.itemId`) and
(b) live SSE (token/reasoning blocks are id-less; persisted items carry `ctx.itemId`), **merged by
deduping on `ctx.itemId`** (`chatStore.ts:15`, enforced `:1455,:2460,:3004`). `blockStream.ts`
handles the claude-sdk MCP **double-event** (inline + post-stream flush) via `seenCallIds`. A streamed
assistant `message` gets its item id **stamped onto the existing streamed `text_done` in place**
(match by responseId+fullText) so reconnect sees it as already rendered. **This is the durable-vs-
streaming merge point.** (TUI equivalent: byte-equal multiset `consume_match`, not itemId — `tui-vs-web.md` Q1.)
→ `cuj-answers/web-client.md` Q3.

### ✅ Stop / interrupt a running turn **[LIVE]**
`POST …/events {type:interrupt}` (only if running and not a child; child stop delegated to parent).
Server adds to `_interrupt_fenced_sessions` and forwards; **interrupt fencing** drops the cancelled
turn's trailing `response.*` output (no persist) but keeps pre-stop narration.
**Trace evidence** (`conv_7fe7513654`, long bash turn cancelled): delivered as a control event on the
**same events endpoint** (`{"queued":false}` = applied immediately); the `agent:claude-sdk` span
carries **`error.type=cancelled`** (`record_cancellation`, `runtime/telemetry.py`) while
`otel.status_code` stays OK — the unambiguous interrupt fingerprint. The in-flight `sleep 30` bash was
abandoned mid-setup (had reached `POST /mcp` + `policy.evaluate TOOL_CALL` + `POST /mcp/execute`); an
`omni-server GET … ERROR` at teardown = stream/tunnel close (interrupt-fencing dropping trailing
output). **Interrupt is NOT a gap** — all in-scope harnesses support the web Stop.
→ `_LIVE-TRACE-FINDINGS.md` "Interrupt"; `server-api-state-streaming.md` Q1 (control events).

### ✅ Browse / view / edit files; terminals; subagents rail **[terminals/subagents partial LIVE]**
**Files** — `FilesPanel.tsx` / `FileViewer.tsx` (Monaco) / `MonacoDiffViewer`; in-browser edit +
autosave; changed-files badge (`GET …/environments/default/changes`). **Terminals** —
`TerminalsPanel.tsx` xterm.js → `WS …/resources/terminals/{tid}/attach` → runner tmux; terminal-first
sessions render inline. **Subagents rail** — `SubagentsPanel.tsx` / `useChildSessions.ts`; tree by
depth; `GET …/child_sessions`; manual create via `AddAgentDialog.tsx`.
**Trace corroboration:** disconnect trace shows `session.resource.created` for `terminal_tui_main`
(tmux_socket/tmux_target); sub-agent trace shows `GET …/child_sessions` (×6).
→ `cuj-answers/web-client.md` Q2; `_LIVE-TRACE-FINDINGS.md` "Disconnect", "Sub-agent spawn".

### ✅ Settings (theme / shortcuts / account); Policies admin page
Settings: theme, keyboard shortcuts, account/password (`accounts_enabled`), archived sessions.
Policies page `pages/PoliciesPage.tsx` (`/policies`, admin): `GET /v1/policy-registry`, `GET/POST/
PATCH /v1/policies`. Capabilities probe `GET /v1/info` gates UI (`CapabilitiesContext.tsx`). Fork/
clone `ForkSessionDialog.tsx`; approve deep-link `pages/ApprovePage.tsx` (`/approve/:sid/:eid`,
pre-auth via `GET /elicitations/{eid}`).
→ `cuj-answers/web-client.md` Q2.

### ✅ TUI / REPL equivalents of the above
`omnigent/repl/_repl.py` (`run_repl`): rich streaming, slash-command menu, `@`-file completer, resume
picker (`_resume_picker.py`), theme picker, event tape (`--debug-events`, Ctrl+E). **TUI has NO
WebSocket usage** — its only push channel is per-session SSE `…/stream`. Slash→API: `/model`→PATCH
`{model_override}`; `/effort`→PATCH `{reasoning_effort}`; `/compact`→POST `{type:compact}`;
`/cancel`→POST `{type:interrupt}`; `/fork`→POST `…/fork`; `/switch` re-points the SSE stream (NOT
`/switch-agent`). Dedup by byte-equal text (not itemId). **⚠️ TUI emits no OTel spans** —
`telemetry.init("omni-tui")` is never called (doc-vs-code mismatch; the httpx propagation mechanism is
in place, just uninitialized).
→ `cuj-answers/tui-vs-web.md` Q2, Q3, Q6.

---

## §2.F  Agents, subagents, executor, routing

Deeper: `cuj-answers/executor-subagents-compaction-cache.md`, `runner-dispatch-mcp.md`,
`architecture/runtime-executor.md`, `runner.md`.

### ✅ The executor's role in the turn loop **[LIVE]**
An `Executor` (`inner/executor.py:518`) is the per-vendor adapter translating **Omnigent's abstract
turn model ↔ a concrete vendor SDK**. In: `run_turn(messages, tools, system_prompt, config)`. Out:
an async stream of `ExecutorEvent`s (`:96-261`: TextChunk, ReasoningChunk, ToolCallRequest,
ToolCallComplete, TurnComplete, CompactionComplete, TurnCancelled, ExecutorError). Runs inside a
**per-conversation harness subprocess** (`omni-harness`); the `ExecutorAdapter`
(`harnesses/_executor_adapter.py:141`) lazily builds the executor, translates the request → Message
list + ExecutorConfig, calls `run_turn`, and per event emits typed SSE.
**Trace evidence** (`conv_63542a5f` / `conv_3a411011`): the loop is exactly the `agent:claude-sdk`
AGENT span wrapping `run_turn()`, with `tool:` calls nested under it.
**Nuance — no `llm_call [LLM]` span for SDK:** `start_llm_span` exists (`tracing.py:223`) but no
`inner/*_executor.py` calls it — the adapter subsumes the LLM call in the AGENT span (verified: zero
`llm`-named spans in Jaeger). Real nesting = agent → tool; policy is a separate `policy.evaluate`
span; a sub-agent is a separate omni-harness-rooted trace.
→ `cuj-answers/executor-subagents-compaction-cache.md` Q1.

### ✅ Spawn subagents **[LIVE]**
Two declaration forms: **`AgentTool`** (by name or inline, `inner/tools.py:266`) and
**`SelfAgentTool`** (clones parent, self-tools removed, `:298`). Runtime spawn (SDK): the LLM calls
**`sys_session_send`** (`spawn.py:56`) → mints a child Conversation + starts a turn (or posts to an
existing child); child inherits the caller's runner (co-location) and runs the same loop; results
return via `async_work_complete`. Native: children minted via `external_subagent_start`.
**Trace evidence** (`conv_387e2405`, debby orchestrator → claude + gpt children): the full mechanism
on `omni-harness` — `tool:sys_session_send` (×2, one per child); children **created via runner→server
`POST /v1/sessions` (×9)** each a **full session with its own `session.id`** (touched ids:
`conv_387e2405` + `conv_9edbfd15` + `conv_3cfe960a`); `tool:sys_read_inbox` (×2, parent drains child
results, confirms consume-once); `GET …/child_sessions` (×6); per-child `agent:<name>` spans with
their **own** `policy.evaluate` gates. The gpt (codex) child fails on creds; the claude child answers
— debby tolerates partial failure. **Beyond code:** a sub-agent is a first-class child Conversation
(own session.id + own runner), **not** an in-process call; stitching a sub-agent tree across traces
requires collecting **all** child session.ids (no single trace spans the whole tree).
→ `cuj-answers/executor-subagents-compaction-cache.md` Q2; `_LIVE-TRACE-FINDINGS.md` "Sub-agent spawn".

### ✅ Information propagation between agents & subagents (#5)
`pass_history:true` snapshots the parent's "self" history as the child's "parent" history;
`pass_histories:[names]` for named snapshots (`inner/tools.py:281`). **Tool args = the child's first
user message** (`spawn.py:281`). Results **truncated** into the `async_work_complete` payload the
parent drains. **Siblings/cross-agent only communicate via the parent** (`sys_session_send` confined
to direct children, `spawn.py:73`). **⚠️ lossy:** `pass_history`/`pass_histories`/`max_sessions` drop
to defaults on the omnigent-compat translator (`spec/omnigent.py:1338`); first-class on the inner
`AgentDef` path.
→ `cuj-answers/executor-subagents-compaction-cache.md` Q3.

### ⚠️ Subagent depth limits — failure-branch (GAP)
**VERIFIED display-only, NO spawn-time cap.** `_MAX_SUBAGENT_TREE_DEPTH=3` (`repl/_repl.py:201`) is
used **only** to cap how deep the REPL sidebar *renders* the child tree (`:6266`) — never consulted at
spawn time. `SelfAgentTool` recursion is bounded only by clone-pruning (one level of `self`); ordinary
`AgentTool` chains can recurse **unbounded**. `AgentTool.max_sessions` (`:295`) is a per-tool
*concurrency* cap, not depth. Real runaway-recursion risk (code comment: "add when needed").
→ `cuj-answers/executor-subagents-compaction-cache.md` Q2; gaps §2.F below.

### ⚠️ Intelligent routing (#10)
`server/smart_routing.py:route_turn` (`:234`): infer harness family (claude/gpt) → LLM judge
classifies cheap/medium/expensive → picks a model from `TIER_TEMPLATES` → applied as `model_override`
(runner gets a concrete model, not a routing config). **⚠️ native harnesses not routable** (returns
None); judge unavailable → fail-open to spec default; hallucinated model → clamp to `tier[0]`. Also an
LLM-classifier **policy** variant (`deny_trivial_to_expensive_model`, `policies/builtins/routing.py`).
→ `CUJ-ANALYSIS.md §2.F`; `policy-enforcement.md` (adjacent).

### ⚠️ Runner dispatch / affinity — failure-branch **[LIVE — binding]**
`RunnerRouter.client_for_conversation` (`routing.py:89`): the conversation's `runner_id` is **hard
affinity — no failover/rebalance**. Binding = atomic CAS `set_runner_id`
(`UPDATE … WHERE runner_id IS NULL`, `sqlalchemy_store.py:1918`). Decision tree: not bound →
`CONFLICT` (`:112`); bound-but-offline → `RUNNER_UNAVAILABLE` (`:231`); harness not in the runner's
hello → `RUNNER_CAPABILITY_MISMATCH` (`:236`). **A bound runner going offline strands the session**
until that exact runner reconnects. Hello set = `[claude-native, claude-sdk, codex, openai-agents,
open-responses, pi]` (`serve.py:582`) — **codex-native not in the default list** (it's CLI/host-driven,
not server-dispatched over the tunnel).
**Trace evidence** (mechanism): `omnigent run --server :7777` creates a session **with a bound
runner**; REST `POST /v1/sessions {agent_id}` creates one with **NO runner** (`runner_id:null`) →
next `/events` → `runner_unavailable`. `host_id` at create is the trigger for the host→runner launch.
→ `cuj-answers/runner-dispatch-mcp.md` Q1; `_LIVE-TRACE-FINDINGS.md` "Driving model & runner binding".

### ✅ Create & store a custom agent (Polly)
`omnigent create` or POST bundle. **Three tiers** (`runtime/agent_cache.py`): **ArtifactStore**
(content-addressed `.tar.gz`, source of truth) → **Agent DB row**
(id/name/bundle_location/version/session_id — session-scoped agents non-null `session_id`, template
null) → **AgentCache** (in-mem `AgentSpec` + on-disk extract, **no TTL**, evict on delete, warm-swap
on update via atomic `_specs[id]=` reassign, version bumps). **Security:** `${VAR}` expanded against
server env **only** for operator template agents (`session_id is None`), never tenant/session agents
(`:66-80`).
→ `cuj-answers/executor-subagents-compaction-cache.md` Q7.

### ✅ How a custom agent's own subagents get initialized
`AgentTool` references a registered agent by name or inline spec; `SelfAgentTool` clones the parent
(self-tools removed) → both become nested `AgentSpec` (`spec/omnigent.py:1090`). Loaded with
**`prune_invalid_sub_agents=True`** (`agent_cache.py:94`; impl `spec/__init__.py:314`): depth-first, a
sub-agent that fails validation on an older server is dropped (WARNING) + removed from
`tools.agents`, so version skew degrades gracefully and the parent still dispatches.
→ `cuj-answers/executor-subagents-compaction-cache.md` Q7.

### ⚠️ Async work / inbox mechanics
`sys_call_async` spawns a bg task → returns an `_AsyncToolHandle`; results auto-drain at the iteration
boundary (`_drain_async_completions`, `async_inbox.py:253`) OR via `sys_read_inbox` mid-turn; topic
`async_work_complete`; **consume-once**. **Subagent-completion delivery (runner side):**
`_on_proxy_stream_end` (`runner/app.py:12519`) pushes the child's output to the parent's inbox
(`_deliver_subagent_completion:7248`) + schedules a parent wake-POST. **⚠️
`sys_cancel_task`/`sys_cancel_async` = NO-OP** — tasks table removed → `task_not_found` for all inputs
(`async_inbox.py:108`).
→ `cuj-answers/executor-subagents-compaction-cache.md` Q4.

### ⚠️ Resume dispatch (which harness gets re-launched?) — carries native-subagent gate #848
`resume_dispatch.py:39 run_resume` is **CLI glue for terminal-native sessions**: reads
`labels.omnigent.wrapper` → `_dispatch_wrapper` (`:201`) maps it → the matching `run_<harness>_native`
(`claude`→`run_claude_native`, `codex`→`run_codex_native`). **No wrapper label (= an SDK session)** →
raises a hint to use `omnigent run --resume` (SDK sessions resume through the normal
create→bind→dispatch path, not `resume_dispatch.py`).
**⚠️ native-subagent-completion gate #848 — VERIFIED at `runner/app.py:12607`:**
`elif not _is_native_harness(conv_id) and not has_buffered:` **excludes every native harness** from
the `status="completed"` delivery — native sub-agent completions silently never reach the orchestrator
(native turns only emit `waiting`/`idle`, `:12580`). 7-reporter cluster: #848 (root), #697, #880,
#1449, #1113, #1589, #1410, #762; open PRs #853/#698/#1593/#1462; partial-in-`main` #1286/#1588/#1446.
→ `cuj-answers/runner-dispatch-mcp.md` Q2; `executor-subagents-compaction-cache.md` (failure branches);
gaps §2.F below.

---

## §2.G  Onboarding, credentials & auth

Creds never surface as spans → **all code** (`traces` worktree, HEAD `60d11673`). Deeper:
`cuj-answers/credentials.md`, `architecture/auth-credentials.md`.

### ✅ First-run setup / provider selection
`omnigent setup` wizard (`onboarding/wizard.py:run_wizard_and_launch:1384`), 3 skippable steps: server
URL → LLM executor auth (API key vs Databricks profile, with detected hints) → default agent path.
Writes `~/.omnigent/config.yaml` (`_save_global_config:1476`). **Ambient detection**
(`onboarding/ambient.py:detect_providers:619`) scans in priority order: env API keys → Vertex → Claude
CLI login (`~/.claude/.credentials.json` + macOS Keychain fallback) → Codex config provider → Codex CLI
login → local Ollama. **Databricks profile aliasing** (`onboarding/setup.py:_alias_profile:190`): a
same-host profile is aliased to the existing one (OAuth cache is host-keyed) → no redundant browser
OAuth. Per-harness: claude→anthropic, codex→openai (`default_provider_for_harness:1126`).
→ `cuj-answers/credentials.md` Q1.

### ✅ LLM credential resolution + refresh
Resolution precedence: spec `executor.auth` → spec `connection` → env → CLI login/profile → ambient
(`spec/types.py:562`, `databricks_executor.py`). **Subscription harnesses** (claude-native/sdk, codex)
deliberately **do NOT inherit a parent's Databricks profile** (would bypass subscription auth,
`spec/omnigent.py:124`). **Refresh:** claude-sdk `_DatabricksBearerAuth.auth_flow`
(`databricks_executor.py:367`) calls `Config.authenticate()` **every request** (SDK serves from
in-mem cache, re-shells CLI near expiry) → **survives ~1h**. Codex gateway re-runs the `auth.command`
shell on an **interval** (`_GATEWAY_AUTH_REFRESH_MS=900_000`=15min, `codex_executor.py:60`) + on 401.
API-key/subscription = **static, no refresh** (vendor-managed for CLI subscription).
**⚠️ live example:** codex Databricks gateway currently **403** (dead OAuth refresh grant) — the real
reason codex/codex-native can't be live-traced.
→ `cuj-answers/credentials.md` Q2, Q3a.

### ⚠️ Runner ↔ server auth + refresh — carries WS-tunnel gap
`_make_auth_token_factory` (`runner/_entry.py:271`): stored OIDC token first (`auth_tokens.json`),
else Databricks OAuth via SDK. **HTTP callbacks** `_RunnerDatabricksAuth.auth_flow` (`:192`): fresh
token **per request**, retry once on 401 **or** Apps 302→/oidc (`:241`), injects `X-Databricks-Org-Id`
→ ✅ survives ~1h. **⚠️ WS tunnel** (`ws_tunnel/serve.py:230`): Bearer set **once at open** in the
handshake `additional_headers` (`:540`), **no per-message refresh**. Mitigations make it mostly
self-healing: re-mints on **every reconnect** (`_refresh_auth_token:284`), on handshake 401 (retry
once, `:326`), and on routine ingress recycles (close 1001/1012/502 reset backoff + re-mint). A token
expiry only bites if the socket stays open past expiry with no recycle and no server-side re-auth
trigger; the next reconnect heals it.
→ `cuj-answers/credentials.md` Q3b; `runner-dispatch-mcp.md` Q5; gaps §2.G below.

### ✅ Client ↔ server auth + refresh
`server/auth.py:resolve_auth_source:193`, `UnifiedAuthProvider:250`. Three modes: **header** (default,
`X-Forwarded-Email`; missing → 401 unless `OMNIGENT_LOCAL_SINGLE_USER=1`); **accounts** (user/pass →
cookie); **oidc** (auth-code+PKCE → cookie). Cookie `__Host-ap_session` (HS256 JWT, **validated every
request** with a TTL cache keyed by HMAC digest, `:387`). CLI `omnigent login` (`cli.py:12146`) writes
the JWT (`0600`, with `expires_at`) — **⚠️ NO background refresh**; expired → re-login. Databricks Apps
stores a **pointer record** (no token; minted fresh) + `?o=`→`X-Databricks-Org-Id` per request.
→ `cuj-answers/credentials.md` Q3c.

### ⚠️ Token refresh in the chat path vs the policy-server path — failure-branch **[VERIFIED FIXED]**
**Chat/active turn** — both refresh per request → survive ~1h: runner callbacks
(`_RunnerDatabricksAuth`) + LLM executor (`_DatabricksBearerAuth`; codex interval shell). ✅
**Policy-hook path (native) — the known bug, VERIFIED FIXED by PR #1439 (merged, commit
`e9561916`):** old behavior = a one-shot bearer snapshotted at hook launch died at ~1h; Apps bounced
it 302→/oidc; PreToolUse (fail-closed phase) **failed CLOSED** while chat kept working. Fix: the hook
re-mints a fresh bearer on 302→/oidc|/.auth **or** 401 and retries once
(`claude_native_hook.py:36-37,525-580,657,729,875`); fail-closed remains the last resort when
unmintable. **Residual (out of scope):** OpenCode plugin snapshot (`runner/app.py:1141-1149`,
`OMNIGENT_POLICY_AUTH`) still one-shot, degrades **fail-OPEN** after ~1h.
→ `cuj-answers/credentials.md` Q4; `policy-enforcement.md` Q5.

### ✅ Caching: what's cached, TTL, invalidation (agents, credentials)
| What | Where | TTL | Invalidation |
|---|---|---|---|
| MLflow model catalog | `onboarding/providers/__init__.py:102` (`_CATALOG_TTL_SECONDS=3600`) | **1h** | TTL |
| Provider model listing (`sys_list_models`) | `model_catalog.py:61` (`_CATALOG_TTL_S=300`) | **5min** | TTL; `clear_model_catalog_cache():250` |
| Provider/credential resolution | — | **none** | fresh per call/spawn |
| Agent bundle (spec + extract) | `runtime/agent_cache.py` | **none** | evict on delete; warm-swap on update |
| Runner DBX SDK auth | `_make_auth_token_factory` closure | per-factory; SDK in-mem, re-shell near expiry | factory rebuild |
| Client cookie → user-id | `server/auth.py:387` (HMAC-keyed) | token remaining lifetime | TTL; ⚠️ no revocation list |
| Native session state / policy token | `bridge.json`/`policy_hook.json` | one-shot snapshot | re-created on relaunch; **re-minted on 401/302 (PR #1439)** for claude/codex native |
→ `cuj-answers/credentials.md` Q5; `executor-subagents-compaction-cache.md` Q8.

---

## §2.H  API & message surface

Deeper: `cuj-answers/server-api-state-streaming.md` Q5, `tui-vs-web.md` Q2, `web-client.md` Q2,
`runner-dispatch-mcp.md` Q4/Q5, `host-channel.md`.

### ✅ Full set of REST calls per component (TUI / WebUI / runner → server) **[partial LIVE]**
**54 routes** on the sessions router (handler names verified by AST extraction). Highlights: Session
CRUD (`POST/GET/PATCH/DELETE /v1/sessions[/{id}]`, `/fork`, `/switch-agent`, `/projects`, `/labels`,
`/read-state`); Turn I/O (`POST …/events`, `GET …/stream`, `GET …/items`); Elicitations
(`POST …/elicitations/{eid}/resolve`, `GET …/elicitations/{eid}`); native hooks
(`…/policies/evaluate`, `…/hooks/permission-request`, `…/hooks/codex-elicitation-request`); Agent
(`…/agent`, `…/agent/contents`, `…/mcp`); Resources (`…/child_sessions`, `…/resources/{environments,
terminals,files}`); Sharing (`…/permissions`, `…/owner`); App-level (`/health`, `/api/version`,
`/v1/me`, `/v1/info`, `/v1/users/search`). **Web** adds a large collaboration/admin surface
(`/comments`, `/policies`, `/auth/*`, `/v1/hosts/{id}/{runners,filesystem,directories}`,
`/v1/runners`); **TUI** calls a strict subset (no `/switch-agent`, no projects/permissions/comments/
policies). **Runner→server** (over the WS tunnel): `/events`, `external_*`, `/policies/evaluate`,
`GET …/items`, `GET …/agent/contents`, `POST /v1/summarize`.
**Trace-confirmed roots** (both corpus convs): `POST /v1/sessions`, `POST …/events`, `GET …/{id}`,
`GET …/stream`, `PATCH …/{id}`, `GET …/agent`, `GET …/items`, `GET …/agent/contents`,
`GET /api/version`, `POST …/policies/evaluate`, `GET …/resources/terminals`, `GET …/skills`.
→ `cuj-answers/server-api-state-streaming.md` Q5; `tui-vs-web.md` Q2; `web-client.md` Q2.

### ✅ Full set of WebSocket / SSE messages per component **[partial LIVE]**
**Client↔server WS:** `WS /v1/sessions/updates` (sidebar; C→S `watch`; S→C snapshot/changed/removed/
heartbeat); `WS …/resources/terminals/{tid}/attach` (xterm↔tmux). **SSE:** `GET …/stream` (the live
tail). **Ingress tunnels:** `WS /v1/runners/{id}/tunnel`, `WS /v1/hosts/{id}/tunnel` (JSON control
frames; not HTTP; manual traceparent inject/extract). **Host↔server** carries `host.*` frames
(`host.stat`, `host.launch_runner`, `host.list_dir`, `host.create_worktree`, …) — ephemeral control
plane (not streamed to client, not persisted; durable side-effects = `set_runner_id`/`set_host_id` +
`hosts` DB). **The TUI has no WebSocket usage at all.**
**Trace evidence:** host-channel trace `aee57ff3…` — `omni-host host.stat`+`host.launch_runner` nest
under `omni-server POST /v1/hosts/{id}/runners` (manual JSON-frame propagation verified).
→ `cuj-answers/host-channel.md`; `server-api-state-streaming.md` Q5.

### ✅ Message durability: which messages stream vs which persist (incl. reasoning)
**DURABLE** (`_extract_persistent_item_from_sse`, `sessions.py:8930`): only
`response.output_item.done` carrying a `message`/`function_call` (status `completed` only)/
`function_call_output`, plus `compaction` items; also `session.resource.created/.deleted` → resource
items and routing decisions. Text deltas are accumulated (`text_acc`) and flushed to a durable message
only at a function-call boundary or terminal `response.*`. **TRANSIENT** (SSE-only): all `session.*`
lifecycle/presence, the `response.*.delta` family, reasoning deltas, the Responses-API turn lifecycle,
`response.elicitation_request/_resolved`, heartbeats. **Reasoning:** streamed as
`response.reasoning_text.delta` (transient); **persisted on native** (vendor mirrors items),
**recomputed/not-stored on SDK** (claude-sdk `compacted_messages` keeps only content blocks, so
thinking is regenerated next turn).
→ `cuj-answers/server-api-state-streaming.md` Q6; `harness-behavior.md` §8.

### ✅ The *entire* set of API requests client (TUI / WebUI) → server, incl. over websocket
Covered by the two bullets above (REST + WS/SSE). **Three SSE/WS naming families:** `response.*`
(turn/output lifecycle, deltas, elicitations — mirrors OpenAI Responses API, 24 literals);
`session.*` (Omnigent session/sidebar/presence lifecycle — status, input.consumed, presence, model,
reasoning_effort, usage, agent_changed, resource.created/.deleted, child_session.updated,
changed_files.invalidated, heartbeat, …); `external_*` (the **input** vocabulary a native forwarder
POSTs into `/events` so the server re-publishes/persists terminal-observed activity — **not** an SSE
output prefix). `_format_sse` = `event:<type>\ndata:<json>\n\n`; terminal nameless `data: [DONE]`.
**Events envelope** (trace-verified): `POST /events {"type":"message","data":{"role":"user",
"content":[{"type":"input_text","text":"…"}]}}`; control `{"type":"interrupt"}` / `{"type":"compact"}`.
→ `cuj-answers/server-api-state-streaming.md` Q6; `web-client.md` Q2; `tui-vs-web.md` Q2.

---

## Reliability-gap findings (carried forward from CUJ-ANALYSIS §6, updated with what we verified)

Grouped by domain. 🔴 P0 / 🟠 P1 / 🟡 P2 (from OSS triage). **VERIFIED** = re-confirmed against
current `main` this pass. Prod is v0.3.0 (2026-06-27); the 06-29 batch is on `main` but not released.

### Session lifecycle, streaming & continuity [§2.A]
- 🔴 **Idle reaper / watchdog kills active turns; native sessions never reaped.** No writers to
  `_in_flight_response_ids`, no `OMNIGENT_HARNESS_IDLE_TIMEOUT` knob. Issues #1414, #1349 (no PR),
  #1528, #1119 · PRs #1420, #1529, #371, #1227.
- 🟠 **Runner tunnel / stream-recovery defects.** #1116 (keepalive-1011 drops tunnels, no PR), #1117,
  #1118, #1026, #1076 · PRs #1198, #1189, #1077 · in `main` #1078.
- **(code) Runner-offline-on-message** — VERIFIED: fresh item with no runner+host → 503 pre-persist
  (`sessions.py:19073`); bound-but-offline → persisted, forward fails, idle, client stuck until
  snapshot reconciliation.
- **(code) Streaming↔durable dedup hinges on `itemId`** — the native FIFO-desync class
  (`server-api-state-streaming.md` Q2); no server item id at native dispatch.
- **(trace) `/compact` on a model-less session errors pre-LLM** — confirms #1192 live (`conv_63542a5f`).
  ⚠️ resume-overflow OMNI-143 (reactive overflow ends the turn, does NOT compact — `runner/app.py:13804`).
  _Interrupt is NOT a gap: all in-scope harnesses support the web Stop (trace `error.type=cancelled`)._

### Model selection [§2.B]
- 🟠 **claude-sdk silently bills Opus when Sonnet was selected.** VERIFIED at
  `claude_sdk_executor.py:1910-1912` (`_DATABRICKS_CLAUDE_DEFAULT_MODEL` fires on None override).
  Issue #1128 · real fix PR #1146 · ⚠️ #1570/#1563 (frontend) do **not** fix it.
- **(code) Native mid-session model override never affects the running turn** — next turn only (all
  four); codex-SDK tears down the thread; claude-native `/effort none|minimal` persist-only (`:11521`).

### Subagents & runner dispatch [§2.F]
- 🔴 **Native sub-agent completions silently never reach the orchestrator.** VERIFIED gate at
  `runner/app.py:12607` (`elif not _is_native_harness(conv_id) and not has_buffered:`). Issues #848
  (root), #697, #880, #1449, #1113, #1589, #1410, #762 · open PRs #853, #698, #1593, #1462 ·
  partial-in-`main` #1286, #1588, #1446.
- **(code) No spawn-time subagent depth cap** — VERIFIED: `_MAX_SUBAGENT_TREE_DEPTH=3` is REPL-render-
  only (`repl/_repl.py:201`); unbounded `AgentTool` recursion.
- **(code) Hard runner affinity, no failover** — VERIFIED: `routing.py:89` never re-routes a bound
  conversation; offline runner → `RUNNER_UNAVAILABLE` strands the session.
- **(code) `sys_cancel_task`/`sys_cancel_async` = no-op** — VERIFIED: tasks table removed
  (`async_inbox.py:108`), `task_not_found` for all inputs.

### Onboarding, credentials & auth [§2.G]
- 🔴 **Managed sandboxes broken under OIDC/accounts auth** (runner tunnel 403; host never boots via
  `nohup` env-prefix). Issues #357, #1305, #1297 · PRs #1298, #360 + #1308 (overlapping tunnel-auth).
- 🟠 **Host daemon can't reach backend behind a corporate proxy** (no `HTTP(S)_PROXY`/`NO_PROXY` in the
  daemon allowlist). Issue #1022 · PR #1029.
- 🟠 **First-run install: Claude CLI via `npm -g` → EACCES.** Issue #890 · PR #891. Also #904, #1023.
- **(code) Policy-hook static token → fail-closed after ~1h** — ✅ **VERIFIED FIXED (PR #1439, merged
  `e9561916`)** for claude-native + codex-native (re-mint on 302/401 + retry-once,
  `claude_native_hook.py:525-580`). **Residual:** OpenCode plugin snapshot (`runner/app.py:1141-1149`)
  still one-shot, **fail-OPEN** after ~1h (out of scope).
- **(code) WS tunnel runner-auth: Bearer injected once at open, no per-message refresh** — VERIFIED
  (`ws_tunnel/serve.py:540`); mitigated by reconnect/recycle re-mint (mostly self-healing).

### Tools / sandbox (OmniBox) [§2.C]
- 🟠 **`credential_proxy` trust-boundary defect (SECURITY):** parent-side `subprocess.run(shell=True)`
  + arbitrary file reads on an unenforced "trusted-spec-only" assumption (`credential_proxy.py:190`).
  Issue #1542 · **no PR**.
- 🟠 **Sandboxed claude-sdk crashes on macOS instead of degrading.** Issue #517 · part-2 flag #541 in
  `main`; part-1 auto-degrade never landed (no PR).
- **(code) Timers unusable on sessions-native** — `NotImplementedError` (`timer.py:220`); works under
  the local-runner topology (trace-confirmed, `conv_48ce846b`).

### Policy / access control [§2.D]
- **(code) Permission store disabled ⇒ `accessible_by=None` returns ALL sessions** — cross-user
  data-leak risk on open/misconfigured servers; `_require_user()` must gate.

### Web UI [§2.E]
- 🟡 **CJK IME: Enter to confirm composition submits prematurely** (data-loss, no workaround). Issue
  #433 · PR #567.
- 🟡 **File viewer / browser gaps:** non-git Changes panel empty #725 (PR #843); browser empty after
  reconnect #386 (PR #578); staged/unstaged filter #951 (PR #1587); mobile HTML preview/download
  #968/#969 (no PR); fullscreen #1464 (no PR).

**✅ Already fixed on `main` since v0.3.0 (not gaps):** #668 macOS 60s timeout (#1546), web_search on
non-OpenAI (#54), markdown preview (#970), Windows (#19/#1236/#1325/#1375), install
aarch64/Intel/gpt-deps (#308/#458/#296), **native hook token fail-closed (#1439)**.
**🚫 Excluded as feature requests:** new harnesses, multi-account features, monolith decomposition,
command-palette/shortcuts. **Dropped as minor:** model-less SDK `/compact` raw error (#1192 — web
shielded by #1139, maintainer leans wont-fix; **but confirmed live-reproducible** this pass).
**Fast wins (PRs written, unreviewed):** #1146, #1029, #891, #1198, #1189, #567.
**No-PR gaps needing fresh code:** #1349, #1116, #517 (part-1), #1542, native-subagent #848 (open PRs
exist but none merged), OpenCode policy-token follow-up.

---

*Companion docs: `architecture/*.md` (per-component), `cuj-answers/*.md` (per-domain deep dives),
`cuj-answers/_LIVE-TRACE-FINDINGS.md` (the 11 live traces). Source-of-truth rule: running code is
ground truth; traces validate/enrich it. Anchors re-derived on `main` HEAD `60d11673`.*
