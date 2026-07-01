# Omnigent Server — Architecture (SME deep-dive)

**Scope:** `omnigent/server/` — the FastAPI control plane. Core is the (912 KB!)
`routes/sessions.py`, plus `routes/runner_tunnel.py`, `routes/host_tunnel.py`,
`routes/terminal_attach.py`, `auth.py`, the conversation store under
`stores/conversation_store/`, the SSE formatter `_format_sse`, the `WS /v1/sessions/updates`
stream, and `/health`.

**Source-of-truth rule applied:** every mechanism below is anchored to `file:line` on
current `main` (worktree `…/traces`). Line numbers in `designs/CUJ-ANALYSIS.md` had drifted
by ~thousands of lines (the file grew); the anchors here were re-derived from the live code.
Trace evidence is from local Jaeger (corpus `conv_b4f2faed…` claude-sdk new+resume,
`conv_63542a5f…` claude-sdk tool-use).

---

## 1. Overview

The server is the **system-of-record and the fan-out hub**. It owns the durable
conversation store (chat.db / Postgres), terminates all client connections (REST + SSE +
two WebSockets), and brokers every turn to a **runner** over a reverse WebSocket tunnel.
It is deliberately **harness-agnostic**: it never talks to an LLM or a vendor CLI directly;
it persists items, forwards events to the runner, and relays the runner's SSE stream back
out to clients while persisting the durable subset.

Three structural facts explain almost everything:

1. **Persist-before-forward (invariant I1).** A client message is written to the
   conversation store **before** it is forwarded to the runner. The store is the source of
   truth; the runner is a recomputable cache.
   (`_forward_event_to_runner`, `sessions.py:8495`, docstring `:8507-8513`.)
2. **Two SSE consumers, one pub-sub.** The runner emits an SSE stream; the server's
   `_relay_runner_stream` (`sessions.py:9371`) consumes it, **persists the durable items**,
   and **re-publishes** every event onto an in-process pub-sub (`runtime/session_stream`).
   Each connected client's `GET /sessions/{id}/stream` (`sessions.py:19190`) is a
   **live-tail subscriber** of that pub-sub — *no buffer, no replay*.
3. **Reconnect = snapshot + live tail (NOT replay).** Because the live stream has no
   buffer, a (re)connecting client reads a **snapshot** (`GET /sessions/{id}`,
   `sessions.py:14132`) for everything that happened before it subscribed, then dedupes the
   overlap by **item id**.

```
            REST + SSE + WS                  reverse WS tunnel
 ┌────────┐  ───────────────►  ┌────────────────┐  ───────────────►  ┌────────┐
 │ client │                    │   OMNI-SERVER  │   HTTP-over-WS     │ runner │→ harness
 │ web/TUI│  ◄───SSE/WS────────│ (control plane)│  ◄───SSE stream────│        │
 └────────┘                    └───────┬────────┘                    └────────┘
                                       │  asyncio.to_thread
                                       ▼
                              conversation store (chat.db / PG)  ← SOURCE OF TRUTH
```

---

## 2. Key files (file:line)

### 2.1 `routes/sessions.py` — the core (54 routes)

The router factory mounts **54** routes (full table in the CUJ doc §"client→server"). Hot paths:

| Mechanism | Symbol | Anchor |
|---|---|---|
| Submit event (the big switch) | `post_event` | `sessions.py:18150` |
| Persist-before-forward (I1) | `_forward_event_to_runner` | `sessions.py:8495` (persist `:8540`, forward `:8697`, consumed `:8704`) |
| Harness-aware dispatch (native bypass) | `_dispatch_session_event_to_runner` | `sessions.py:8735` |
| Runner SSE consumer + durable persist | `_relay_runner_stream` | `sessions.py:9371` |
| Which SSE events become DURABLE | `_extract_persistent_item_from_sse` | `sessions.py:8930` |
| SSE wire formatter | `_format_sse` | `sessions.py:1848` |
| Live-tail generator (+`[DONE]`) | `_stream_live_events` | `sessions.py:11229` (`[DONE]` at `:11341`) |
| SSE endpoint | `stream_session` | `sessions.py:19190` |
| Snapshot endpoint | `get_session` → `_get_session_snapshot` | `sessions.py:14132`, builder `SessionResponse(` at `:2458` |
| Disconnect detector (long-poll) | `_poll_request_disconnect` | `sessions.py:1183` |
| WS sidebar | `session_updates` | `sessions.py:14530` |
| Status publish (single funnel) | `_publish_status` | `sessions.py:5343` |
| input.consumed publish | `_publish_input_consumed` | `sessions.py:2537` |
| Interrupt fence set | `_interrupt_fenced_sessions` | def `:931`, drop in relay `:9464` |
| Status cache (sidebar bridge) | `_session_status_cache` | `:837` |
| Create session | `create_session` | `sessions.py:13688` |
| Fork | `fork_session` | `sessions.py:15180` |
| Switch agent | `switch_session_agent` | `sessions.py:15415` |
| PATCH (rename/archive/model/effort/runner) | `update_session` | `sessions.py:14795` |
| Delete | `delete_session` | `sessions.py:19358` |
| Native policy HTTP hook | `evaluate_policy` | `sessions.py:15973` |

### 2.2 Tunnels & auth

- **`routes/runner_tunnel.py`** — `WS /v1/runners/{runner_id}/tunnel` (`tunnel`, `:278`).
  Runner dials *out* to the server; server registers it in the `TunnelRegistry` and makes
  RPC HTTP calls *back* through the socket. Token-binding + owner resolution (`:294-371`)
  before `accept()`. Server→runner client is built in `RunnerRouter._client_for_runner`
  (`runner/routing.py:243`) on a custom `WSTunnelTransport` (`:256`), explicitly
  telemetry-instrumented (`:264`) because the global httpx hook can't see the custom transport.
- **`routes/host_tunnel.py`** — `WS /v1/hosts/{host_id}/tunnel` (`tunnel`, `:121`). JSON
  control frames (`HostFrameKind`: launch_runner, stop_runner, fs ops…). `_receive_loop`
  `:369`, `_sender_loop` `:356`, `_ping_loop` `:552`.
- **`routes/terminal_attach.py`** — `WS …/terminals/{terminal_id}/attach` (`:131`). The
  server **shuttles raw frames** browser↔runner (`_shuttle_ws_frames`, `:346`; two pumps
  `_browser_to_runner` `:368`, `_runner_to_browser` `:386`). Authorized at `_authorize_terminal_attach` `:259`.
- **`auth.py`** — `AuthProvider` ABC (`:237`); `UnifiedAuthProvider` (`:250`), one of
  `header | oidc | accounts` per deploy (`OMNIGENT_AUTH_PROVIDER`). `get_user_id` (`:333`)
  reads a trusted header (default `X-Forwarded-Email`) **or** the `__Host-ap_session` HS256
  cookie. Loopback single-user maps missing identity to the reserved `"local"` sentinel.

### 2.3 Conversation store (`stores/conversation_store/`)

- `append` (`sqlalchemy_store.py:1411`) — the persist primitive. Locks the conv row
  (`_lock_conversation`, FOR UPDATE on PG), bumps `updated_at`, allocates O(1) `position`
  from a maintained `next_position` counter, mints a **fresh `item_id`** per item
  (`generate_item_id`, `:1479`), stores `response_id` (turn grouping) and `created_by`,
  status `"completed"` (items are final on append), and inserts an FTS row.
  **No content-based dedup at the store** — dedup is by item id downstream.
- `set_runner_id` (`sqlalchemy_store.py:1918`) — **atomic CAS**:
  `UPDATE … WHERE id=:id AND runner_id IS NULL`. Exactly one concurrent first-dispatch wins
  (rowcount==1); the loser re-reads to find the winner. This is the runner-binding race guard.
- `fork_conversation` (`:2266`), `switch_conversation_agent` (`:2576`) — see CUJ doc.

---

## 3. Data flow — the turn lifecycle (POST /events → SSE back to client)

```mermaid
sequenceDiagram
  participant C as Client (web/TUI)
  participant S as omni-server (post_event)
  participant DB as conv store (chat.db/PG)
  participant P as policy.evaluate (in-proc)
  participant R as omni-runner (via WS tunnel)
  participant H as harness

  Note over C: opens GET /sessions/{id}/stream FIRST (live-tail), reads snapshot
  C->>S: POST /v1/sessions/{id}/events {type:"message", role:"user"}
  S->>S: _require_access_and_level(LEVEL_EDIT); validate type ∈ _ALLOWED_EVENT_TYPES
  S->>P: _evaluate_input_policy → engine.evaluate  (span: policy.evaluate)
  alt policy DENY/ASK
    P-->>S: deny(reason)
    S->>DB: _persist_policy_deny_sentinel
    S-->>C: SSE response.completed (terminal) + session.status idle
    S-->>C: 200 {queued:false, denied:true, reason}
  else ALLOW
    S->>DB: conversation_store.append([user item])   ⟵ I1 PERSIST-BEFORE-FORWARD
    S->>R: POST /v1/sessions/{id}/events {type, …, persisted_item_id, model_override}
    R-->>S: 202 (turn started as background task)
    S-->>C: SSE session.input.consumed {item_id}     ⟵ only after forward OK
    S-->>C: 202 {queued:true, item_id}
    Note over S,R: meanwhile _relay_runner_stream is tailing R's GET /stream
    R->>H: drive harness; emit SSE
    H-->>R: response.created / .output_text.delta* / .output_item.done / .completed
    R-->>S: same SSE frames (over tunnel)
    S->>S: re-publish each to session_stream (live clients)
    S->>DB: persist DURABLE subset (output_item.done, compaction, resource_event…)
    S-->>C: SSE response.* (live tail)
    R-->>S: session.status idle  →  S: _publish_status(idle) + cache write
  end
```

**Real spans seen** (`tree cfb59197f6f92755…`, the tool-use conv's POST /events trace, 415
spans): `POST /v1/sessions/{session_id}/events` → `policy.evaluate` (child, with its own
SELECTs) → `UPDATE`/`INSERT chat.db` (the append) → `POST` (cross-service edge
`omni-server → omni-runner [POST /v1/sessions/{conversation_id}/events]`) → the runner then
calls **back** `GET /v1/sessions/{id}/items` and `GET /v1/sessions/{id}` (transcript
reconstruction). The persist (`INSERT`/`UPDATE`) precedes the runner `POST` in wall-clock
order — I1 visible in the trace.

### Forward-failure branch (runner offline / tunnel dropped)

In `_forward_event_to_runner` the `httpx.post` is wrapped in try/except: on failure the
item **stays persisted**, the server logs "runner picks up on reconnect", and publishes
`session.status idle` (`sessions.py:8705-8711`). For a fresh item event where **no runner is
bound at all**, `post_event` raises `RUNNER_UNAVAILABLE` (503) *before* persist
(`sessions.py:19073`) — except the host-relaunch path, which first tries to relaunch a runner
on the still-online host (`:18920-19006`).

---

## 4. Channels & event/message types

### 4.1 The four client↔server transports

| Transport | Route | Direction | Purpose |
|---|---|---|---|
| REST | 50+ paths under `/v1/sessions/…`, `/v1/me`, `/v1/info`, `/health`, `/api/version` | C→S | commands + reads |
| SSE | `GET /v1/sessions/{id}/stream` | S→C | live tail of one session |
| WS | `WS /v1/sessions/updates` | C↔S | sidebar (watch-set → snapshot/changed/removed/heartbeat) |
| WS | `WS …/terminals/{tid}/attach` | C↔S→R | raw xterm shuttle to runner tmux |

(Plus server-side ingress WS: `WS /v1/runners/{id}/tunnel`, `WS /v1/hosts/{id}/tunnel`.)

### 4.2 SSE wire format

`_format_sse` (`sessions.py:1857`): `event: <type>\ndata: <json>\n\n`. The terminal
sentinel is a **nameless** frame `data: [DONE]\n\n` (`:11341`). Every published dict is
validated against the `ServerStreamEvent` discriminated union
(`_SERVER_STREAM_EVENT_ADAPTER`, `:826`) at the wire boundary — an unmodelled `type` fails
loud rather than shipping.

### 4.3 Event taxonomy — STREAM (transient) vs DURABLE

The union itself is **labeled** in source (`schemas.py:3724-3782`):

- **DURABLE** (persisted to conversation history): exactly **`response.output_item.done`**
  (`OutputItemDoneEvent`) — wraps a conv-store item; the comment literally says
  *"Persistent (POST + SSE replay) — wraps conv-store items"*. Plus `compaction` events and
  resource lifecycle events, which the relay persists as their own conv items
  (`_extract_persistent_item_from_sse` `:8930`; `_resource_event_item_from_sse` `:9005`).
- **TRANSIENT (SSE-only, never persisted):** all `session.*` lifecycle
  (`session.status`, `.input.consumed`, `.presence`, `.model`, `.reasoning_effort`,
  `.usage`, `.agent_changed`, `.todos`, `.resource.created/.deleted`,
  `.child_session.updated`, `.changed_files.invalidated`, `.heartbeat`, …); the delta
  events (`response.output_text.delta`, `response.reasoning_text.delta`,
  `response.reasoning.started`, `response.reasoning_summary_text.delta`); the
  Responses-API turn lifecycle (`response.created/.in_progress/.completed/.failed/`
  `.cancelled/.queued/.incomplete`); and operational signals (`response.error/.retry`,
  `response.compaction.*`, `response.elicitation_request/_resolved`).

**Naming convention:** three families —
`response.*` (turn/output lifecycle, mirrors OpenAI Responses API),
`session.*` (Omnigent session/sidebar/presence lifecycle), and
`external_*` (events a *native* harness's forwarder POSTs **into** `/events` so the server
re-publishes/persists them — input vocabulary, not output; see §4.4).

### 4.4 `POST /events` input vocabulary (`_ALLOWED_EVENT_TYPES`, `sessions.py:800`)

= every conversation item type (`message`, `function_call`, `function_call_output`,
`compaction`, …) **plus** control/external types: `interrupt`, `approval`,
`mcp_elicitation`, `compact`, `stop_session`, and the `external_*` family
(`external_assistant_message`, `external_conversation_item`, `external_output_text_delta`,
`external_output_reasoning_delta`, `external_session_interrupted`,
`external_session_superseded`, `external_elicitation_resolved`, `external_session_status`,
`external_session_usage`, `external_compaction_status`, `external_model_change`,
`external_reasoning_effort_change`, `external_session_todos`, `external_subagent_start`,
`external_codex_subagent_start`, `external_codex_collaboration_mode_change`). Unknown type → 400.

### 4.5 WS /sessions/updates frames

- C→S: `{"type":"watch","session_ids":[…]}` (full-replace watch-set; deduped, capped at
  `_SESSION_UPDATES_MAX_WATCHED=500`).
- S→C: `{"type":"snapshot","items":[SessionListItem…]}` (per watch), `{"type":"changed",…}`,
  `{"type":"removed","ids":[…]}`, `{"type":"heartbeat"}`. Watched rows are **pull-based**
  (re-read + diff every `_SESSION_UPDATES_RESCAN_INTERVAL_S=4s`); newly-accessible sessions
  are **push-based** via `_discovery` listening on `user_session_stream`
  (`_announce_session_added`). Trace context is injected into every frame in `_send`
  (`:14601`) and extracted in `_reader` (`consume_frame_span("session_updates.watch")`, `:14683`).

---

## 5. Trace evidence (concrete spans/edges observed)

From the saved corpus (local Jaeger, `service.name` ∈ {omni-server, omni-runner,
omni-harness}); each span carries `session.id=<conv_…>`:

- **Server FastAPI roots** (every client REST/SSE call is its own root span):
  `POST /v1/sessions`, `POST /v1/sessions/{session_id}/events`,
  `GET /v1/sessions/{session_id}`, `GET /v1/sessions/{session_id}/stream`,
  `PATCH /v1/sessions/{session_id}`, `GET /v1/sessions/{session_id}/agent`,
  `GET /v1/sessions/{session_id}/items`, `GET /api/version`. Confirms the route surface.
- **`policy.evaluate`** child spans appear inline under `POST /events` (6× in the PONG conv,
  5× in tool-use), with attributes `policy.phase` / `policy.decision` / `policy.reason` /
  `session.id` (`engine.py:255-279`). Matches the in-process policy choke point.
- **Cross-service edges** (server↔runner over the WS tunnel, all carried by
  `HTTP /v1/runners/{runner_id}/tunnel websocket send|receive` spans):
  - `omni-server → omni-runner`: `POST /v1/sessions` (session init, ×5), `POST /v1/sessions/{conversation_id}/events` (turn forward, ×2), `GET /v1/sessions/{session_id}` (×2), `GET …/resources/terminals` (×4), `GET …/skills` (×1).
  - `omni-runner → omni-server` (the runner calling **back** during transcript rebuild /
    policy): `GET /v1/sessions/{session_id}/items` (×6), `GET …/agent/contents` (×6),
    `POST /v1/sessions/{session_id}/policies/evaluate` (×4 — the native policy hook),
    `GET /v1/sessions/{session_id}` (×3), `PATCH /v1/sessions/{session_id}` (×2),
    `GET /api/version`.
- **DB spans** dominate counts (`SELECT`/`PRAGMA`/`UPDATE`/`INSERT chat.db`) and nest under
  the owning request span — visible `INSERT`/`UPDATE` under `POST /events` is the I1 persist.
  (Per the brief, treat `PRAGMA`/`connect` as noise.)
- **Decoupled roots:** the runner's own `GET …/stream http send` spans root their own traces
  (the SSE forwarder is request-decoupled). They're stitched to the rest only by
  `session.id` — exactly the cross-trace grouping the OBSERVABILITY design calls out (§8).

---

## 6. Per-harness differences (server's view)

The server is mostly harness-agnostic, but `post_event` / `_dispatch_session_event_to_runner`
branch on **SDK vs native**:

- **claude-sdk / codex (SDK):** server is the single writer. `_dispatch_…` → `_forward_event_to_runner`
  → **persist user item, forward, publish `input.consumed`** (`sessions.py:8915-8926`). The
  runner's SSE comes back through `_relay_runner_stream`, which persists `output_item.done`
  as durable items. Transcript is 100% server-owned. *Live trace: yes (corpus).*
- **claude-native / codex-native:** `_is_native_terminal_session` branch
  (`sessions.py:8817`). Web-typed user messages are **NOT persisted server-side** — they're
  forwarded into tmux and the **transcript forwarder becomes the single writer** (it later
  POSTs `external_conversation_item`). Server records an optimistic `pending_inputs` entry
  (`:8851`) replayed into the snapshot and drained on `input.consumed`
  (`cleared_pending_id`). Control events (`interrupt`/`stop_session`/`compact`) are
  forwarded harness-agnostically; the runner dispatches by harness (native injects into the
  pane). Native model/effort overrides are best-effort (`external_model_change` /
  `external_reasoning_effort_change` persist to columns + publish `session.model` /
  `session.reasoning_effort`). Status comes via `external_session_status` →
  `_publish_status`. *codex-native: no live trace (creds expired); covered by code +
  CUJ-ANALYSIS §4 + analogy to claude-native (claude-native corpus exists).*
- **polly / custom agents:** run on an SDK harness (typically claude-sdk) → identical
  server path to claude-sdk; the only difference is the agent bundle.

---

## 7. Failure branches & gaps

- **Runner offline on message:** item persisted, forward fails → `session.status idle`
  published, runner picks up on reconnect (`:8705`). But if no runner ever rebinds, the
  client's optimistic bubble can sit until a snapshot reconciliation. For a *brand-new* item
  with no runner bound, the server 503s before persist (`:19073`) unless the host can
  relaunch.
- **Hard runner affinity, no failover.** A pinned runner going offline →
  `RUNNER_UNAVAILABLE`/`CONFLICT` ("resume the session to bind a registered runner")
  (`routing.py:110-116, 230-235`). Strands the session until reconnect.
- **`failed` is sticky vs trailing `idle`** (`_publish_status` `:5389`) — a `failed` edge is
  not overwritten by a follow-on `idle`; cleared only by the next `running`. Native-specific
  guard (StopFailure → failed, then PTY-activity idle ~1s later).
- **WS tunnel runner-auth** is established once at handshake; token-binding proves *a* token
  but in no-allowlist mode any non-empty token derives a valid runner id — owner resolution
  (`runner_tunnel.py:348-371`) closes the owner-less-runner cross-tenant gap.
- **Permission store disabled ⇒ `accessible_by=None`** returns all sessions
  (cross-user leak risk on misconfigured open servers) — CUJ-ANALYSIS §6 [§2.D].
- **policy.evaluate span is created in the engine, not the HTTP handler.** The native HTTP
  hook `evaluate_policy` (`sessions.py:15973`) has **no** explicit span of its own; the
  `policy.evaluate` span comes from `engine.evaluate` (`engine.py:255`) called inside it. So
  in a trace, the native policy hook shows as the FastAPI request span
  `POST …/policies/evaluate` with a `policy.evaluate` child.

---

## 8. Open questions

1. **Relay restart semantics.** `_relay_runner_stream` exits on 45 s heartbeat gap; what
   precisely re-spawns it (`_ensure_runner_relay_ready`) and is there a window where durable
   `output_item.done` events are missed during a relay flap? (Affects local↔server divergence.)
2. **Dedup symmetry.** Server appends a fresh `item_id` and forwards `persisted_item_id` so
   the runner drops its own pre-resolution copy by id (`sessions.py:8618`). On the
   native bypass there is *no* server item id (only `pending_id`) — confirm the client's
   FIFO/stableKey dedup is the only thing preventing a double bubble there.
3. **`_session_status_cache` is per-process.** On a multi-replica deploy, does the sidebar's
   `list_sessions`/`/health` read a status written by a *different* replica's relay? (Cache
   is in-memory, `:837`.)
4. **`refresh_state=true` snapshot** re-pulls live runner overlays — what is the full set of
   overlays it refreshes vs. what stays in the AP-process caches?
