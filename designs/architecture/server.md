> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Server (FastAPI control plane + conversation store + DB)

Anchors are `path:line` in `/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`
(`main` + telemetry PR #1617). All opened & confirmed unless tagged `(unverified)`.
`S` = `omnigent/server/routes/sessions.py` (20629 lines).

## 1. Role & boundaries

The server (`omni-server`) is the **source of truth and the only writer of durable
conversation state**. It owns:
- **Conversation store + DB** (`omnigent/stores/conversation_store/`, `omnigent/db/`):
  conversations, conversation_items, conversation_labels, session_permissions.
- **The REST/SSE/WS surface** clients talk to (55 OpenAPI paths + 2 WS + 1 SSE).
- **Request lifecycle**: validate → policy → **persist-before-forward** → forward to the
  bound runner → relay the runner's SSE back, persisting durable items as they stream.
- **Runner/host tunnel termination**: runners and hosts dial *in* over a WS tunnel; the
  server tunnels HTTP-over-WS to them (`WSTunnelTransport`). It does NOT open sockets to
  runners.
- **Affinity**: `conversations.runner_id` (the bound runner) via `PATCH /sessions/{id}`.
- **Ephemeral live state**: `_session_status_cache`, presence, pending-elicitation counts,
  interrupt fences, read-state — all in-memory, single-replica (§7).

It does **NOT**: run the LLM, hold the harness/transcript-of-record for native harnesses
(the native CLI's own session file is authoritative; the server mirrors via the
forwarder), sandbox/exec (OmniBox lives runner-side), or persist most stream events
(only `OutputItemDoneEvent` items are durable — §6).

## 2. Key files & entrypoints (verified)

- `S:18150 post_event` — `POST /v1/sessions/{id}/events`, the dispatch hub (~1000 lines).
- `S:8495 _forward_event_to_runner` — **invariant I1**: persist (`8540`) then forward (`8697`),
  `session.input.consumed` published AFTER forward succeeds (`8704`).
- `S:8735 _dispatch_session_event_to_runner` — native-message single-writer bypass vs persist.
- `S:9371 _relay_runner_stream` — subscribes runner SSE, persists durable items, republishes
  to local `session_stream` (`9810`); exits → `_publish_status(failed, runner_disconnected)` (`9823`).
- `S:8930 _extract_persistent_item_from_sse` — which SSE events become durable items.
- `S:11229 _stream_live_events` — client SSE generator: ready heartbeat, interval heartbeat,
  `[DONE]` in `finally` (`11341`), presence connect/disconnect, `ServerStreamEvent` validation.
- `S:19190 stream_session` — `GET /v1/sessions/{id}/stream` (live-tail only, snapshot-on-connect).
- `S:14531 session_updates` — `WS /sessions/updates` (sidebar watch/snapshot/changed/removed).
- `S:14132 get_session` → `_get_session_snapshot` — durable snapshot (`_build_session_response` `S:2352`).
- `S:13688 create_session`; `S:15189 fork_session`; `S:15423 switch_session_agent`;
  `S:14800 update_session` (runner-binding LWW); `S:19363 delete_session`.
- `S:5343 _publish_status` (the single status funnel); `S:5777 _get_runner_client`.
- `omnigent/server/routes/runner_tunnel.py:277 tunnel` — `WS /v1/runners/{id}/tunnel`.
- `omnigent/server/routes/host_tunnel.py`, `terminal_attach.py`, `session_policies.py`.
- `omnigent/server/schemas.py:3724 ServerStreamEvent` — the SSE discriminated union (§6).
- DB/store (from sub-agent map): `Conversation` dataclass `omnigent/entities/conversation.py:185`;
  `ConversationItem` `…:699`; ids `omnigent/db/utils.py:619/639`; tables `omnigent/db/db_models.py:236/452/513/195`.

## 3. Internal model

**Durable (DB):**
- `conversations` (`db_models.py:236`): `id` (`conv_<hex>`, `db/utils.py:619`), `agent_id`,
  `runner_id` (nullable, no FK — the affinity pin), `host_id`, `parent_conversation_id`,
  `root_conversation_id` (NOT NULL), `kind` (`default`/`sub_agent`), `title`, `model_override`,
  `reasoning_effort`, `cost_control_mode_override`, `harness_override`, `sub_agent_name`,
  `external_session_id` (native CLI's own session id), `next_position`, `session_state`,
  `session_usage`, `workspace`, `git_branch`, `archived` (Boolean column, **not** a label).
- `conversation_items` (`db_models.py:452`): `id` (`msg_`/`fc_`/`fco_`/`err_`/… + hex,
  `db/utils.py:639`), `response_id` (turn group), `position`, `type`, `data`, `created_by`,
  `search_text`. **Unique only on `(conversation_id, position)`** (`db_models.py:497`) — there is
  **no uniqueness/dedup constraint on item id or any client-supplied id**.
- `conversation_labels` (`db_models.py:513`): PK `(conversation_id, key)`, upsert-only,
  `value` String(256). Carries runner-owned/native overlays:
  `omnigent.last_task_error_{code,message}` (`S:502`), fork directives
  (`omnigent.fork.source_id`, `FORK_CARRY_HISTORY`), codex collaboration mode, etc.
- `session_permissions` (`db_models.py:195`): PK `(user_id, conversation_id)`, `level`
  1=read/2=edit/3=manage/4=owner.
- **No `tasks` table** (dropped, migration `b9c1d2e3f4a5`). Status is NOT durable; it is derived
  from the relay-fed cache (§6).

**Ephemeral (in-process, lost on restart, single-replica):**
- `_session_status_cache: dict[str,str]` (`S:837`) — idle/running/waiting/failed; the *only*
  source of "busy" (`S:12701`). Rederivable from the runner.
- `_session_background_task_count_cache` (`S:857`) — sticky bg-shell tally; refreshes only at a
  turn boundary (documented KNOWN LIMITATION `S:847`).
- `_interrupt_fenced_sessions: set[str]` (`S:931`) — sessions whose Stopped turn's trailing
  `response.*` the relay must drop.
- `_read_last_seen` / `_read_explicit_unread` (`S:869`) — per-user read state, **no durable
  source**, resets on restart (accepted tradeoff `S:863`).
- `presence` (per-tree viewer set, `omnigent/server/presence.py`), `pending_elicitations`
  (sidebar badge counts).

## 4. Inter-component channels (every edge in/out)

```
        REST(JSON) + SSE                  WS tunnel (HTTP-over-WS, runner dials in)
client ───────────────► SERVER ◄════════════════════════════════════ RUNNER
   ▲   GET .../stream (SSE)   │  POST /v1/sessions (notify+turn-start)   │
   │   WS /sessions/updates   │  GET .../items, .../agent/contents       │
   └───────────────────────────  POST .../events (interrupt/stop/result) │
                              │  GET .../resources/terminals, .../skills │
                              │  POST .../mcp/execute                    │
                              └──► HOST (WS tunnel: POST /hosts/{id}/runners launch)
RUNNER ──callbacks──► SERVER: POST .../events (external_*, transcript mirror, status, usage),
   POST .../policies/evaluate, GET .../items, PATCH .../{id} (bind/labels), POST .../mcp
```

| Peer | Transport | Direction / ops | Durable? |
|---|---|---|---|
| **Client↔Server** | REST JSON | `POST /events` (input/interrupt/approval/stop/compact), session CRUD, fork, switch-agent, snapshot `GET /sessions/{id}`, items, policies | input persisted (I1); rest as noted |
| Client←Server | **SSE** `GET /sessions/{id}/stream` | `response.*` deltas + `session.*` lifecycle; live-tail only, no replay | only `output_item.done` durable |
| Client↔Server | **WS** `/sessions/updates` | client→ `{watch, session_ids}`; server→ `snapshot`/`changed`/`removed`/`heartbeat` | pull-diff of durable list rows |
| Client↔Server | **WS** `terminal_attach.py` | tmux PTY attach for native terminal sessions | n/a (transient bytes) |
| **Server↔Runner** | **WS tunnel** `/v1/runners/{id}/tunnel` (`runner_tunnel.py:277`) | HTTP-over-WS both ways; runner dials in, server `WSTunnelTransport` issues HTTP | tunnel for all server↔runner HTTP |
| Server→Runner | tunneled REST | `POST /v1/sessions` (notify+start), `POST .../events` (forward turn / interrupt / tool_result / compact / stop / external_session_status forward), `GET .../resources/terminals` (snapshot), `GET .../skills`, `POST .../mcp/execute` | — |
| Runner→Server | tunneled REST (callbacks) | `POST .../events` with `external_*` (native transcript mirror, status, usage, subagent_start, todos), `POST .../policies/evaluate`, `GET .../items` + `GET .../agent/contents` (cold-cache history reload), `PATCH .../{id}` (bind runner_id / cost labels), `POST .../mcp` (proxy) | items/labels durable |
| **Server↔Host** | **WS tunnel** `host_tunnel.py` | `POST /v1/hosts/{id}/runners` launches a runner; `on_runner_connect` binds it | — |

**Trace evidence** (corpus, grouped by `session.id`; one conv = many trace_ids because the
SSE/response path is decoupled from the request — confirmed in all summaries):
- **claude-sdk turn** `conv_32db…`: `server→runner POST /v1/sessions ×3` (start), `runner→server
  GET .../items ×4 + .../agent/contents ×4` (cold-cache history reload), `runner→server
  POST .../policies/evaluate ×2`, `runner→harness POST .../events ×3`, WS-tunnel
  `receive ×71 / send ×21`. Captured `policy.evaluate` payloads show input + assistant-output +
  request-context evaluations (server-side enforcement on both paths).
- **claude-native turn** `conv_94e6…`: `runner→server POST .../events ×14` — dominant edge is the
  **native transcript forwarder** posting `external_*` events back (the single-writer mirror),
  plus `claude_native.forward ×4` / `claude_native.inject ×1`. Confirms native input is NOT
  AP-persisted on the way in; it round-trips back as `external_conversation_item`.
- **subagent spawn** `conv_fc47…`: `runner→harness POST .../events ×10`, `POST .../mcp/execute ×3`
  + `POST .../mcp ×3` (Omnigent MCP proxy), `GET .../child_sessions ×1`, `policy.evaluate ×14`.
  Payloads show `sys_session_send`→worker→`sys_read_inbox` (Polly-style delegation through the
  inbox).

## 5. CUJ behaviors (server's part)

**Request lifecycle (the happy path), `POST /events type=message`:**
```
post_event(S:18150)
 ├ auth + LEVEL_EDIT (_require_access_and_level)            S:18229
 ├ validate type ∈ _ALLOWED_EVENT_TYPES (S:800); parse data S:18245
 ├ INPUT policy (S:18321) — DENY/ASK ⇒ publish deny sentinel, persist sentinel,
 │   response.completed, idle; return {queued:False,denied:True}  S:18347
 ├ resolve runner (_get_runner_client S:5777); if none: managed-launch rendezvous /
 │   host relaunch / wait-for-runner; else persist-failure-turn (native) or 503  S:18889-19076
 ├ _dispatch_session_event_to_runner(S:8735):
 │   • non-native → _forward_event_to_runner: APPEND item (I1, S:8540) → POST runner /events
 │     (S:8697) → publish session.input.consumed w/ persisted id (S:8704)
 │   • claude-native message → BYPASS persist; record pending_inputs optimistic id;
 │     _forward_native_terminal_message (tmux inject); forwarder mirrors back later  S:8817-8914
 └ return {queued:True, item_id|pending_id}
relay (_relay_runner_stream S:9371): runner SSE → persist durable items
   (_flush_relay_text at tool boundaries, _extract_persistent_item_from_sse) → publish to
   local session_stream → client SSE renders.
```

**Streaming vs durable, incl. reasoning:** SSE `response.output_text.delta` /
`response.reasoning_text.delta` / `reasoning.started` are **transient** (SSE-only); the relay
accumulates text deltas (`S:9544`) and flushes them as a durable `message` item at each
text→tool boundary and on terminal events (`_flush_relay_text S:9270`). **Reasoning deltas are
not persisted as items**; the durable counterpart is the assistant `message` text (reasoning is
streamed for live view only). Tool calls/outputs persist via `output_item.done` (status
`completed` only, `S:8987`). `external_output_reasoning_delta` (`S:18636`) is explicitly transient.

**Dedup (server side):** the server does NOT dedup at the DB (no unique item-id constraint). It
emits the anchors clients dedup against:
- `session.input.consumed` (`S:2537`) carries the persisted `item_id` (+ `cleared_pending_id` to
  drop a native optimistic bubble) → client replaces its temp/optimistic bubble by id.
- `_flush_relay_text` republishes flushed text as `output_item.done` *with the store-assigned id*
  so the client stamps it on the already-rendered streamed block (web by content at boundaries /
  TUI by byte-equal segment, `S:9287-9301`).
- **Runner** dedups on cold-cache reload via `persisted_item_id` (`S:8618`): it drops the
  pre-resolution copy by id and appends its own resolved copy.

**Working vs idle:** computed solely from `_session_status_cache` (`S:12701`: running/waiting ⇒
busy). Every write funnels through `_publish_status` (`S:5343`) which (a) writes the cache and (b)
publishes a typed `session.status` SSE. `failed` is sticky against a trailing `idle` (`S:5389`);
cleared on next `running`. Native harnesses: the forwarder POSTs `external_session_status`
(`S:18667`) → `_publish_status` + forward to runner; PTY-activity idle carries no bg count. Snapshot
projects `busy` + `last_task_error` from cache+labels (`S:12700`).

**Disconnect→reconnect / close-page-and-return:** SSE is live-tail only (no buffer, `S:11240`).
Reconnect contract = `GET /sessions/{id}` snapshot (durable items + status from cache + last_task_error
labels) **then** open a fresh `/stream`; events in the gap are deduped client-side by item id.
`/stream` emits a ready `session.heartbeat` at slot registration (`S:11314`), interval heartbeats
(`S:11313`), `[DONE]` on every exit (`S:11341`), and a snapshot-on-connect of terminals/child-sessions/
presence (`S:19246`). The Databricks Apps ingress ~5-min HTTP/2 cap is handled by transparent client
reconnect, NOT defeated server-side (`S:19348`). Closing the page just drops the SSE/WS; returning
re-runs snapshot+stream. ⚠️ `_session_status_cache`/read-state are in-memory: a server restart mid-turn
loses live status (rederived once the runner re-emits) and resets read/unread.

**Fork (`POST /sessions/{source}/fork`, `S:15189`):** deep-copies items (optionally
`up_to_response_id`, `S:15379`), clones the source/target agent into a **new session-scoped agent**
inside the store transaction (`S:15279`), grants OWNER, `_announce_session_added`. Model settings carry
over only within the same provider family (`_same_provider_family S:15319`). Native targets get a
`FORK_CARRY_HISTORY` label so the runner rebuilds its native transcript from the copied items
(cross-family always rebuilds; same-family may clone the rollout, `S:15323-15352`). ⚠️ Fork is a
**top-level deep copy**, NOT a parent/root pointer — lineage is only the `omnigent.fork.source_id`
label (corrects a likely CUJ-ANALYSIS assumption). Runner is bound afterward by `PATCH .../{id}`.
Fork conv `conv_151ad…` has **0 spans** in the corpus (no turn was run on it post-fork).

**Switch-agent (`POST .../switch-agent`, `S:15423`):** in-place, **idle-only (409 if running,
`S:15481`)**; deletes+re-clones the agent, clears `external_session_id`, recomputes presentation labels,
publishes `session.agent_changed` (`S:15604`), and resets runner-side resources after responding
(`S:14614`). Same session/transcript/host/files. ⚠️ no-op switch to same bundle rejected (`S:15510`).

**Archive / delete:** archive = owner-only `PATCH .../{id} {archived}` (`S:14831`), a Boolean column;
prunes read-state. Delete (`S:19363`) owner-only; best-effort runner-side resource cleanup (404-tolerant),
optional `?delete_branch=true` worktree removal, then deletes the row (CASCADE drops items/labels/perms).

**Resume (new runner) / runner-binding:** `PATCH .../{id} {runner_id}` (`S:14800`) is the affinity
primitive — create-bind/resume-bind/recover-bind all send the live runner id; **owner-only**,
**last-write-wins** (`S:14809`), `""` clears. On resume the server reloads NO transcript itself; the
re-bound runner pulls history on demand via `GET .../items` (paginated, `order=asc`, `limit≤1000`,
`S:16584`) + `GET .../agent/contents` (the ×4–×6 edges in every trace). For native, the CLI's own session
file is authoritative and the runner replays from `external_session_id`/copied items.

**Interrupt fencing:** `type=interrupt` (`S:18439`) publishes `session.interrupted`, adds the session to
`_interrupt_fenced_sessions`, forwards `{type:interrupt}` to the runner (5s); if delivery fails the fence
is lifted so remaining output isn't dropped (`S:18462`). While fenced, the relay drops the turn's trailing
`response.*` (no persist/forward) except elicitation lifecycle (`_FENCE_EXEMPT`, `S:944`); the fence lifts
on the next `running` or any terminal `response.*` (`S:9464`). `stop_session` (`S:18467`) is the same
fence + harness-agnostic runner kill (raises on failure, owner-only) + stops a host-spawned runner; it is
non-sticky so the next message relaunches.

⚠️ **Failure branches:** runner unreachable + non-native item ⇒ 503 (must not persist what the harness
won't see, `S:19073`); runner unreachable + **native** message ⇒ persist a failure turn so it survives
reload (`S:19040`); policy-eval crash ⇒ fail-closed DENY (`S:18331`); relay transport lost ⇒
`session.status=failed{runner_disconnected}` (`S:9823`); compact returns non-204/200 from runner ⇒ error,
no AP-side fallback (`S:18591`).

## 6. Answers to the doc questions (server scope)

- **Persist-before-forward (I1):** `_forward_event_to_runner` appends (`S:8540`) before POSTing the
  runner (`S:8697`); `input.consumed` only after the forward returns (`S:8704`). The native bypass is the
  one exception (single-writer: forwarder is sole history writer, `S:8767`).
- **Streaming vs durable surface (`ServerStreamEvent`, `schemas.py:3724`):** the union is explicitly
  partitioned — **only `OutputItemDoneEvent` is "POST + SSE replay" (durable)**; everything else is
  SSE-only: `session.*` lifecycle (`status/usage/model/reasoning_effort/agent_changed/todos/
  terminal_pending/sandbox_status/skills/input_consumed/interrupted/created/superseded/presence`),
  resource lifecycle, token deltas (`output_text.delta`, `reasoning.*`), Responses-API turn lifecycle
  (`created/queued/in_progress/completed/failed/cancelled/incomplete`), `compaction.*`, heartbeats,
  elicitation request/resolved.
- **Dedup:** server has **no DB-level dedup**; it provides item-id anchors (`input.consumed`, flushed-text
  `output_item.done`) and the runner uses `persisted_item_id` for cold-cache id-based dedup (§5).
- **Working/idle:** `_session_status_cache` + `_publish_status` (§5); sticky-failed; native via
  `external_session_status`.
- **API surface (REST vs SSE vs WS):**
  - **REST** (55 paths, `<SP>/openapi.json`): session CRUD (`/v1/sessions[...]`), `/events`, `/items`,
    fork, `/switch-agent`, `/agent[...]`, `/agent/mcp-servers`, `/policies` + `/policies/evaluate`,
    `/comments`, `/resources/{terminals,files,environments,shell,search}`, `/permissions`, `/read-state`,
    `/child_sessions`, codex_goal, hosts/runners, `/health`, `/api/version`, `/v1/me`.
  - **SSE**: `GET /v1/sessions/{id}/stream` — the `ServerStreamEvent` union above.
  - **WS**: `/v1/sessions/updates` (sidebar), `/v1/runners/{id}/tunnel`, host tunnel, terminal-attach.
- **Sidebar fetching:** `WS /sessions/updates` replaces the old 4s poll: client sends `{watch,
  session_ids}`; server snapshots then diffs watched rows each `_SESSION_UPDATES_RESCAN_INTERVAL_S`
  (`S:14692`); *discovery* of new/forked/shared sessions is push-based via `user_session_stream`
  → `changed` (`S:14717`). Idle list = zero HTTP. `GET /v1/sessions` (`S:14241`) deliberately omits
  per-row liveness (`S:14389`).
- **Inbox / subagents:** server mints child sessions from `external_subagent_start` (`S:18827`) and
  `sys_session_send`; sub-agent terminal status forwards to the parent's runner inbox, healing a stale
  child `runner_id` via the parent's live runner (`_recover_subagent_status_forward_via_parent`, `S:18752`).
- **Policy enforcement (server-level):** evaluated in `post_event` BEFORE persist/forward on both input
  and output paths (`S:18296`); runner-side tool gates call back via `POST .../policies/evaluate`
  (corpus: `policy.evaluate` spans). Fail-closed on evaluator error (`S:18331`).
- **Smart routing / model & effort:** first-message model routing in `_forward_event_to_runner`
  (`S:8653`, persists `model_override`); mid-session model/effort via `PATCH .../{id}`
  (`S:14917/14900`) or terminal-observed `external_model_change`/`external_reasoning_effort_change`
  (`S:18800/18808`). Per-event `model_override` wins, else the persisted column (`S:8631`).

## 7. Reliability gaps / sharp edges (confirmed in code)

1. **All live status/presence/read/fence state is in-memory & single-replica** (`S:837,857,869,931`).
   Multi-replica AP: a stream on replica B never sees a status published on replica A (the cache and
   `session_stream` are process-local); a server restart mid-turn shows stale/lost "working" until the
   runner re-emits, and silently resets all read/unread. The code documents status as "rederivable from
   the runner" (`S:863`) but read-state and the interrupt fence have **no durable source**.
2. **Interrupt fence leak window:** if interrupt/stop delivery fails the fence is lifted (`S:18465`), but if
   the runner dies *after* fencing without emitting a terminal `response.*` or `running`, the fence is only
   cleared in the relay `finally` via discard — a fence set by replica A is invisible to replica B.
3. **Background-task tally only refreshes at a turn boundary** (`S:847`): a shell that exits while idle can
   show "N background tasks running" in chat/sidebar/reload until the next message.
4. **`failed`→sticky relies on the assumption no flow does a legit `failed`→`idle`** (`S:5389`); a new
   harness that does would have its error erased.
5. **Forward-after-persist soft failure:** if the runner POST fails after the item is persisted,
   `_forward_event_to_runner` logs, publishes `idle`, and returns the id (`S:8705`) — the user message is in
   the store but the turn never started and no error item is written (silent no-op until the next message).
6. **No DB dedup on items** (`db_models.py:497`): correctness depends entirely on client/runner id-based
   dedup discipline; a buggy forwarder that re-posts an `external_conversation_item` would double-persist.
7. **Snapshot/stream race is client-reconciled, not server-ordered:** events between snapshot read and
   stream subscribe are only deduped if the client honors item-id; the server gives no sequence/cursor on
   the SSE stream (live-tail has no replay/`Last-Event-ID`).

## 8. Corrections to CUJ-ANALYSIS

(Line numbers in CUJ-ANALYSIS.md drift; these are claim-level corrections from confirmed code.)

1. **§2.A request lifecycle — the "create-or-steer task" / tasks-table model is stale.** There is **no
   `tasks` table** (dropped, migration `b9c1d2e3f4a5`; comments at `S:12700`, `S:14343`). A turn is started by
   `POST runner /v1/sessions/{id}/events` returning 202; the runner runs it as a background task and streams
   via `GET /stream`. "Busy" comes only from the relay-fed `_session_status_cache` (`S:12701`), not a task row.
   Any anchor describing an in-process task object / `_create_and_start_task` / `_steer_active_task` is gone —
   the steer-vs-create branch is now entirely runner-side (the server forwards harness-agnostically).

2. **§2.E (status/dedup) — "server dedups by itemId / `session.input.consumed` consumed-set" is
   imprecise.** The server enforces **no** item-id uniqueness (only `(conversation_id, position)`,
   `db_models.py:497`). `session.input.consumed` (`S:2537`) is a *client* dedup anchor (carries the persisted
   id so the client swaps its optimistic bubble); cold-cache dedup is the **runner's**, keyed on
   `persisted_item_id` (`S:8618`). If the analysis claims a server-side "consumed item id set", that does not
   exist.

3. **§5 (fork/resume) — two corrections.** (a) **Fork is a top-level deep copy, not a parent/root chain**:
   `parent_conversation_id`/`root_conversation_id` are for sub-agents; fork lineage is only the
   `omnigent.fork.source_id` label and copied items get fresh ids (sub-agent map of
   `omnigent/stores/conversation_store/__init__.py:23`, `fork_conversation` at `S:15366`). (b) **The server
   loads no transcript into the runner on resume**; the re-bound runner pulls history via `GET .../items` +
   `GET .../agent/contents` (the ×4–×6 edges in every corpus trace), and native targets replay from their own
   session file / copied items driven by the `FORK_CARRY_HISTORY` label (`S:15338`). Resume-bind itself is just
   a **last-write-wins** `PATCH .../{id} {runner_id}` (`S:14809`), not a CAS.
