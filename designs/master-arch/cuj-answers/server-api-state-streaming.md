# CUJ Answers — Server: API, State & Streaming

Domain: the Omnigent **server** (`omnigent/server/`). Every claim is anchored to `file:line`
on current `main` (worktree `…/traces`) and, where possible, to live Jaeger spans.
Harness scope: claude (sdk + native), codex (sdk + native — no live trace, creds expired),
polly (= custom agents on an SDK harness, behaves as claude-sdk).

> Reading note: `designs/CUJ-ANALYSIS.md` §2.A/§2.E line numbers had drifted by thousands of
> lines vs the live 912 KB `sessions.py`. The anchors below were re-derived from code.

---

## Q1. Full request lifecycle of `POST /v1/sessions/{id}/events`

Handler `post_event` (`sessions.py:18150`). Returns **202**; body is a small ack dict.

**Order of operations (the happy "message" path):**

1. **AuthZ.** `_get_user_id` → `_require_access_and_level(LEVEL_EDIT)` (`:18228-18231`). 404 if
   no conversation (`:18233-18239`).
2. **Validate type.** `body.type ∈ _ALLOWED_EVENT_TYPES` else 400 (`:18245`). Item types also
   `parse_item_data`-validated (`:18282`); client tool specs validated (`:18291`).
3. **Closed-session guard.** A `sys_session_close`d sub-agent rejects new user input → 409
   (`is_session_closed`, `:18306-18314`).
4. **Policy eval (BEFORE persist/forward).** `_evaluate_input_policy` (`:18321`) for user
   messages; output/tool-call policies for assistant/function_call. On exception → treat as
   **deny** so the session can't hang "working forever" (`:18331-18346`).
   - **Deny/ask path:** publish `session.status running`, `_publish_policy_deny`, persist a
     **deny sentinel** item (`_persist_policy_deny_sentinel`, `:18354`), publish a **terminal
     `response.completed`** so headless `-p` live-tail consumers unblock
     (`_publish_input_deny_terminal`, `:18363`), publish `session.status idle`, return
     `{queued:false, denied:true, reason}` (`:18347-18369`). **Not forwarded to the runner.**
5. **Control events (NOT persisted as items):**
   - `interrupt` (`:18439`): publish `session.interrupted`, **add to
     `_interrupt_fenced_sessions`**, POST `{type:interrupt}` to runner (5 s); if undelivered,
     drop the fence (else the still-running turn's output would be lost). `{queued:false}`.
   - `stop_session` (`:18467`): **owner-only** (extra `LEVEL_OWNER` gate `:18472`); fence;
     forward harness-agnostically (`_stop_session_via_runner`, **raises** on failure unlike
     interrupt); if host-spawned, also stop the host runner so the tunnel drops honestly.
     Non-sticky (next message relaunches). `{queued:false}`.
   - `approval` (`:18519`): `_resolve_elicitation` (sets server-side Future, clears badge,
     forwards to runner) + apply deferred policy-ask writes. `{queued:false}`.
   - `mcp_elicitation` (`:18533`): mint `elicit_<hex>`, publish `response.elicitation_request`
     to this stream + ancestor streams, return the id so the runner parks. `{queued:false}`.
   - `compact` (`:18570`): forward to runner first (native injects `/compact` into tmux); only
     if runner 204/absent does the server run its own `_run_compact_locked`. `{queued:false}`.
   - `external_*` family (`:18617-18860`): persist or publish the terminal-observed signal
     (assistant message / conversation item / status / model change / subagent start / …)
     **without starting a turn**. These are the native forwarder's input vocabulary.
6. **Item event — PERSIST-BEFORE-FORWARD (invariant I1).** Resolve a runner client
   (`_get_runner_client`, with managed-launch rendezvous + host-relaunch grace,
   `:18889-19006`). If a *fresh* item event has **no runner and no host** → raise
   `RUNNER_UNAVAILABLE` (503) **before persisting** (`:19068-19076`) so the store never
   desyncs from harness state. Otherwise `_dispatch_session_event_to_runner` (`:19137`):
   - SDK / non-native → `_forward_event_to_runner` (`:8495`):
     **`conversation_store.append([item])` first (`:8540`)**, resolve `file_id`s, build
     `runner_body` (incl. `persisted_item_id`, `model_override`, `harness_override`,
     `has_mcp_servers`), **POST to runner (`:8697`)**, then **`_publish_input_consumed`
     (`:8704`)** carrying the persisted `item_id`. On forward failure: item stays persisted,
     publish `session.status idle` (`:8711`).
   - native-terminal → bypass: do **not** persist; record `pending_inputs` entry, forward into
     tmux; the transcript forwarder is the single writer (`:8817-8914`).
   Return `{queued:true, item_id}` (+ `pending_id` for native).

**Trace evidence** (`tree cfb59197f6f92755…`, tool-use conv, POST /events root, 415 spans):
`POST /v1/sessions/{session_id}/events` → child `policy.evaluate` → `UPDATE`+`INSERT chat.db`
(the append) → `POST` cross-edge to `omni-runner [POST /v1/sessions/{conversation_id}/events]`.
The append's writes precede the runner POST — **I1 visible on the wire.** The runner then
calls back `GET /items` + `GET /sessions/{id}` (transcript rebuild).

**Failure branches:** policy-deny (persist sentinel, no forward); runner-offline-mid-session
(persist, forward fails, idle, picks up on reconnect); runner-never-bound (503 pre-persist);
host-relaunch (the message *is* the relaunch trigger — `:18920-19006`).

---

## Q2. Server-side dedup (how `itemId` / `response_id` are used)

**There is no content-based dedup at the server store.** `append` (`sqlalchemy_store.py:1411`)
mints a **fresh globally-unique `item_id`** per item (`generate_item_id`, `:1479`). Dedup is
**id-based and happens downstream** (runner + client), seeded by ids the server assigns:

- **`item_id`** — the canonical durable id. The server forwards it to the runner as
  **`persisted_item_id`** in `runner_body` (`sessions.py:8618`). On a cold runner cache the
  runner reloads history (which includes this item in *pre-resolution* form), **drops it by
  id**, and appends its own resolved copy — "id-based dedup, not a role/content guess"
  (comment `:8614-8617`). `input.consumed` carries the same `item_id` so the client promotes
  its optimistic bubble to the durable block by id.
- **`response_id`** — the **turn-grouping** key (stored on every item, `append` `:1483`). The
  relay assigns all items of one turn the same `response_id` from the latest
  `response.in_progress` (`_extract_persistent_item_from_sse`, `:8948-9000`); the web UI groups
  them into one bubble and **pairs a `function_call` with its `function_call_output`** even
  when a later `response.in_progress` has advanced `current_response_id`
  (`tool_call_response_ids` map, `_relay_runner_stream` `:9410, :9560-9580`).
- **Native bypass:** no server item id at dispatch — only a `pending_inputs` `pending_id`
  (`sessions.py:8851`). Dedup against the mirrored-back transcript copy relies on the client's
  FIFO/stableKey + the `cleared_pending_id` on `input.consumed` (`_publish_input_consumed`
  `:2554-2569`). This is the FIFO-desync bug class (CUJ-ANALYSIS §6).
- **Telemetry id derivation:** a turn's `response_id` (`resp_<32hex>`) deterministically seeds
  the **root trace id** via `trace_id_from_response_id` (telemetry.py) — so the server's
  per-turn trace is recoverable from the response id (OBSERVABILITY §5.2).

---

## Q3. "working/idle" status — computed and published server-side

**Single funnel:** `_publish_status(session_id, status, error?, response_id?, background_task_count?)`
(`sessions.py:5343`). `status ∈ {idle, running, waiting, failed}` (Pydantic-validated;
non-conforming values fail loud, `:5354-5357`). It does two things atomically:

1. **Writes the in-memory `_session_status_cache`** (`:5391`; cache def `:837`) — the bridge
   the **sidebar** reads. The snapshot/list endpoints read this cache so the sidebar badge
   stays coherent with the SSE stream (docstring `:5359-5365`: "without this, the
   `external_session_status` path leaves the sidebar stuck idle while chat shows Working…").
   Also maintains `_session_background_task_count_cache` (sticky background-shell tally).
2. **Publishes a `session.status` SSE event** (`SessionStatusEvent`, `:5406-5419`).

**Where status edges originate:**
- **SDK turn:** the **runner** emits `session.status` on its SSE stream;
  `_relay_runner_stream` re-publishes via `_publish_status` so it gets `conversation_id` +
  cache write (`:9447-9519`). `running` at turn start, `idle`/`failed` at end.
- **Native:** the forwarder POSTs `external_session_status` → `post_event` validates +
  `_publish_status` (`:18667-18726`). PTY-activity drives `running`/`idle`; StopFailure →
  `failed`.
- **Deny path:** `post_event` publishes `running` then `idle` around the sentinel (`:18352-18364`).

**Stickiness invariant:** a cached `failed` is **not** overwritten by a trailing `idle`
(`:5389-5390`); only the next `running` clears it (and a new turn / failure clears the bg
tally). Prevents the native StopFailure→failed edge from being erased by the ~1 s-later
PTY-idle.

**Presence** (who's *viewing*) is separate from status. Holding `GET /sessions/{id}/stream`
open registers the caller as a viewer (`presence.connect`, `_stream_live_events` `:11307`);
co-viewers receive `session.presence` edges; the snapshot-on-connect includes the full viewer
list (`stream_session` `:19322`). Scoped to the **session-tree root** so viewers of different
sub-agents in one tree see each other. The `?idle=` query param is the viewer's tab-backgrounded
flag; an idle flip arrives as a **reconnect** carrying the new value (no separate endpoint,
`:19216-19219`).

`/health` (`app.py:1659`) reports **liveness** (`runner_online` strict, `host_online`),
not status — the sidebar polls it (batched `?session_ids=`) for the connection dot.

---

## Q4. Disconnect → reconnect contract (snapshot + live tail, NOT replay)

**The live stream has no buffer.** `_stream_live_events` (`sessions.py:11229`) yields events
"from the moment `subscribe` is invoked forward — no buffer, no replay. Events published
before this generator subscribed are lost; clients reconcile pre-subscribe state via the
snapshot endpoint and dedupe by item id" (docstring `:11240-11244`).

**Reconnect sequence (the contract):**
1. Client opens `GET /sessions/{id}/stream` (`stream_session`, `:19190`). The subscribe call
   passes `ready_event={"type":"session.heartbeat"}` yielded **immediately** after the
   subscriber slot registers (`:11314`) so the client has a concrete "I'm now tailing" ack
   before posting a fast one-shot turn. A `pre_ready_snapshot` hook captures in-flight
   assistant text **synchronously** at slot registration (`:11320`) so window deltas don't
   double-render.
2. Client reads the **snapshot** `GET /sessions/{id}` (`get_session` → `_get_session_snapshot`,
   `SessionResponse` built at `:2458`). It carries: identity/status/`background_task_count`,
   paginated `items` (or `[]` if `include_items=false` — the web hydrates the transcript via
   `GET /items` in parallel), `runner_online`/`host_online` (or `None` when sourced from
   `/health`), **`pending_elicitations`** (outstanding approval cards replayed — `:2502`,
   incl. child-session elicitations `:2333-2345`), **`pending_inputs`** (un-consumed native
   web messages replayed `:2511`), cost/usage, `last_task_error`, model/effort/harness.
3. **Dedup the overlap by item id.** Events that fired *before* the snapshot are in the
   snapshot's `items`; events *after* arrive on the live tail. The client drops the duplicate
   by `ctx.itemId` (web `lib/blockStream.ts`, CUJ-ANALYSIS §2.E). This is the snapshot↔stream
   merge point.

**`[DONE]` on every exit path.** `_stream_live_events`'s `finally` always yields
`data: [DONE]\n\n` (`:11341`) so well-behaved SSE consumers see a clean termination on
disconnect, server shutdown, or normal completion. The pub-sub auto-cleans the subscriber
slot in its own `finally`.

**`_poll_request_disconnect`** (`sessions.py:1183`) is the **long-poll** disconnect detector
used by parked routes (notably the claude-native `evaluate_policy`/permission-request hook):
it **blocks on `request.receive()`** for `http.disconnect` rather than polling
`is_disconnected()`, because Claude closes its HTTP request when its TUI prompt is answered
first — without this the handler would sit out the full timeout. (Deliberately a blocking
receive, not the poll variant, so an external `Task.cancel()` always propagates — docstring
`:1193-1203`.) The SSE generator itself uses `request.is_disconnected()` checked on each event
arrival (`_stream_live_events` `:11323`), backstopped by 15 s heartbeats so a half-open socket
is noticed.

**Interrupt fencing across a reconnect.** `_interrupt_fenced_sessions` (set, `:931`) is
populated on `interrupt`/`stop_session` (`:18442, :18476`). In `_relay_runner_stream`
(`:9464-9475`) a fenced session **drops the cancelled turn's trailing `response.*` output**
(no forward, no persist) — but **keeps `text_acc`** so pre-stop narration the user already
watched still persists at the terminal flush. The fence lifts on the next
`session.status running` (a new turn) or any **terminal** `response.*`
(`_TERMINAL_RESPONSE_EVENT_TYPES` — completed = the stop lost the race, process normally).
This guarantees a stopped turn's output never lands in the durable store to resurface on
reconnect.

---

## Q5. The FULL set of client→server requests

54 routes are mounted by the sessions router (handler names verified by AST extraction).
Grouped by purpose:

### Session CRUD / lifecycle
| Method | Path | Handler | Purpose |
|---|---|---|---|
| POST | `/v1/sessions` | `create_session` (`:13688`) | create (JSON=existing agent; multipart=bundled session-scoped agent) |
| GET | `/v1/sessions` | `list_sessions` (`:14236`) | sidebar list (cursor-paginated, `?search_query`, `?project`, archived filter) |
| GET | `/v1/sessions/projects` | `list_session_projects` (`:14061`) | implicit projects (reserved `omni_project` label) |
| GET | `/v1/sessions/{id}` | `get_session` (`:14132`) | **snapshot** (reconnect) |
| PATCH | `/v1/sessions/{id}` | `update_session` (`:14795`) | rename / **archive (owner)** / model_override / effort / cost-mode / runner rebind / labels |
| DELETE | `/v1/sessions/{id}` | `delete_session` (`:19358`) | **owner-only** delete (+`?delete_branch` worktree) |
| POST | `/v1/sessions/{src}/fork` | `fork_session` (`:15180`) | fork (deep-copy items, `up_to_response_id`, agent/harness switch) |
| POST | `/v1/sessions/{id}/switch-agent` | `switch_session_agent` (`:15415`) | **idle-only (409)** in-place agent swap |
| GET | `/v1/sessions/{id}/labels` | `get_session_labels` (`:14192`) | label read |
| PUT | `/v1/sessions/{id}/read-state` | `put_read_state` (`:14089`) | per-user unread tracking |

### Turn I/O
| Method | Path | Handler | Purpose |
|---|---|---|---|
| POST | `/v1/sessions/{id}/events` | `post_event` (`:18150`) | **the turn entrypoint** (message/control/external) |
| GET | `/v1/sessions/{id}/stream` | `stream_session` (`:19190`) | **SSE live tail** |
| GET | `/v1/sessions/{id}/items` | `list_session_items` (`:16578`) | paginated transcript (web hydration; runner transcript rebuild) |

### Elicitations / approvals
| POST | `/v1/sessions/{id}/elicitations/{eid}/resolve` | `resolve_elicitation` (`:18013`) | dedicated resolve URL (also via `approval` event) |
| GET | `/v1/sessions/{id}/elicitations/{eid}` | `get_elicitation` (`:18083`) | fetch one (pre-auth `/approve/…` deep-link) |

### Native-harness hooks (runner / vendor CLI → server, over the tunnel)
| POST | `…/policies/evaluate` | `evaluate_policy` (`:15973`) | proto policy hook (claude PreToolUse/PostToolUse) |
| POST | `…/hooks/permission-request` | `claude_permission_request_hook` (`:15629`) | claude-native permission long-poll |
| POST | `…/hooks/codex-elicitation-request` | `codex_elicitation_request_hook` (`:16205`) | codex-native |
| POST | `…/hooks/antigravity-elicitation-request` | `antigravity_elicitation_request_hook` (`:16278`) | (out of scope) |
| POST | `…/hooks/cursor-permission-request` | `cursor_permission_request_hook` (`:16371`) | (out of scope) |
| POST | `…/hooks/native-permission-request` | `native_permission_request_hook` (`:16478`) | generic native |

### Agent / contents
| GET | `…/agent` | `get_session_agent` (`:19807`) · PUT `update_session_agent` (`:19927`) | session-scoped agent spec |
| GET | `…/agent/contents` | `get_session_agent_contents` (`:19850`) | bundle contents (runner fetches these) |
| POST | `…/mcp` | `mcp_proxy` (`:20040`) | MCP proxy passthrough |

### Resources (terminals / files / environments / child sessions) — all proxied to the runner
`…/child_sessions` (`:16639`), `…/resources` (`:16719`), `…/resources/environments[...]`
(`:17034, 17053, 17672, 17707, 17753, 17778, 17810 GET/PUT/PATCH/DELETE, 17954 shell`),
`…/resources/terminals` (`:17075 list, :17107 create, :17189 get, :17210 transfer,
:17292 delete`), `…/resources/files` (`:17340 list, :17384 upload, :17478 meta,
:17511 content, :17559 delete`), `…/resources/{rid}` (`:17991`).

### Sharing / permissions
`PUT …/permissions` (`grant_permission`, `:19517`), `DELETE …/permissions/{uid}`
(`revoke_permission`, `:19579`), `GET …/owner` (`:19627`), `GET …/permissions` (`:19651`).

### WebSockets (client↔server)
- `WS /v1/sessions/updates` (`session_updates`, `:14530`) — **sidebar**. C→S `watch`; S→C
  `snapshot`/`changed`/`removed`/`heartbeat`.
- `WS …/resources/terminals/{tid}/attach` (`attach_terminal_by_resource_id`,
  `terminal_attach.py:131`) — raw xterm shuttle to runner tmux.

### App-level (outside the sessions router)
`GET /health` (`app.py:1659`), `GET /api/version` (`:1727`), plus `/v1/me`, `/v1/info`
(capabilities probe), `/v1/users/search`, accounts/oidc auth routes, policy-registry, and the
server-side ingress tunnels `WS /v1/runners/{id}/tunnel` (`runner_tunnel.py:278`) and
`WS /v1/hosts/{id}/tunnel` (`host_tunnel.py:121`).

**Confirmed by trace** (root spans across both corpus convs): `POST /v1/sessions`,
`POST …/events`, `GET …/{id}`, `GET …/stream`, `PATCH …/{id}`, `GET …/agent`,
`GET …/items`, `GET …/agent/contents`, `GET /api/version`,
`POST …/policies/evaluate`, `GET …/resources/terminals`, `GET …/skills`.

---

## Q6. STREAM vs DURABLE; SSE event naming

**DURABLE (persisted to conversation history)** — decided by `_extract_persistent_item_from_sse`
(`sessions.py:8930`): only **`response.output_item.done`** carrying a `message` /
`function_call` (status `completed` only — interim statuses are skipped, `:8987`) /
`function_call_output`, plus `compaction` items. The union literally tags this variant
*"Persistent (POST + SSE replay) — wraps conv-store items"* (`schemas.py:3753-3754`).
The relay also persists **resource lifecycle** events
(`session.resource.created/.deleted` → `resource_event` conv item, `:9653`) and **routing
decisions** (`:9681`) so a reconnecting client rediscovers them in the snapshot. Text deltas
are accumulated (`text_acc`, `:9544`) and **flushed to a durable message** only at a
function-call boundary (`:9586-9597`) or any terminal `response.*` (`:9618-9634`) — so the
*streamed* deltas themselves are transient, but the *final* text is durable.

**TRANSIENT (SSE-only, never persisted):** everything else — `session.*` lifecycle &
presence, the `response.*.delta` family, reasoning events, the Responses-API turn lifecycle
(`response.created/.in_progress/.completed/.failed/.cancelled/.queued/.incomplete`),
`response.elicitation_request/_resolved`, `response.error/.retry`, `response.compaction.*`,
heartbeats. (Full membership: `ServerStreamEvent` union, `schemas.py:3724-3782`, with explicit
"Transient (SSE-only)" / "Persistent" section comments.)

**Naming — three families:**
- **`response.*`** — turn/output lifecycle, deltas, elicitations (mirrors the OpenAI Responses
  API surface). 24 literals incl. `response.output_text.delta`, `.output_item.done`,
  `.reasoning_text.delta`, `.completed`, `.failed`, `.cancelled`, `.elicitation_request`.
- **`session.*`** — Omnigent session/sidebar/presence lifecycle (`session.status`,
  `.input.consumed`, `.presence`, `.model`, `.reasoning_effort`, `.usage`, `.agent_changed`,
  `.todos`, `.resource.created/.deleted`, `.child_session.updated`,
  `.changed_files.invalidated`, `.heartbeat`, `.created`, `.superseded`,
  `.terminal_pending`, `.sandbox_status`, `.skills`, `.model_options`, `.collaboration_mode`,
  `.terminal.activity`).
- **`external_*`** — *input* vocabulary a native forwarder POSTs into `/events`
  (`_ALLOWED_EVENT_TYPES`, `sessions.py:800-822`) so the server re-publishes/persists
  terminal-observed activity (assistant message, conversation item, status, model/effort
  change, usage, compaction status, subagent start, …). **Not an SSE output prefix** — these
  are how native turns get *into* the server.

`_format_sse` (`:1857`): `event: <type>\ndata: <json>\n\n`; terminal nameless `data: [DONE]`.
Reasoning: streamed as `response.reasoning_text.delta` (transient); persisted on native (the
forwarder mirrors it as items), recomputed/not-stored on SDK unless the harness emits an
`output_item.done` for it.

---

## Q7. Create / fork / switch-agent / archive / delete / close endpoints

(Detailed branch logic from a focused read of each handler.)

- **Create** — `POST /v1/sessions` (`create_session`, `:13688`). JSON binds an existing
  `agent_id` → returns `SessionResponse`; multipart (`metadata` + `bundle`) creates a
  session-scoped agent + conv row in one txn → `CreatedSessionResponse`. Grants caller
  `LEVEL_OWNER` and `_announce_session_added` (push to other tabs via WS discovery). CSRF:
  `require_json_or_multipart_content_type` + `require_trusted_origin` (`:13683-13686`).
- **Fork** — `POST /v1/sessions/{src}/fork` (`fork_session`, `:15180`), status 201, gate
  `LEVEL_READ`. **Cannot fork a sub-agent** (400, `:15238`). `fork_conversation`
  (`sqlalchemy_store.py:2266`) deep-copies items with fresh ids preserving
  `position`/`response_id`; `up_to_response_id` truncates; **drops instance-scoped labels**
  (native bridge ids, context-token metrics) and does **not** copy
  `external_session_id`/`workspace`/`git_branch`; optional cross-family harness switch resets
  model/effort; native targets carry history via `FORK_CARRY_HISTORY` label. Grants owner +
  announces.
- **Switch agent** — `POST /v1/sessions/{id}/switch-agent` (`switch_session_agent`, `:15415`),
  gate `LEVEL_EDIT`. **Idle-only — 409 if status `running`** (`:15481`). Loads the target
  bundle **before** committing (fail-closed). `switch_conversation_agent`
  (`sqlalchemy_store.py:2576`) deletes the old session-scoped agent, clones the target,
  repoints `agent_id`, resets model/effort on cross-family, **clears `external_session_id`**
  (`:2663`) so the new harness cold-starts next turn, and stamps a **switch-back** label.
  Publishes `session.agent_changed`; resets runner resources in the background.
- **Archive / unarchive** — `PATCH /v1/sessions/{id}` with `archived` (`update_session`,
  `:14795`). **Owner-only** (`required_level=LEVEL_OWNER` when `archived` is set, `:14823`).
  `archived` is a plain DB column (`SqlConversation.archived`), **not** a label/title marker;
  archived rows are hidden from list by default. (Same handler does rename via `title`,
  model/effort/cost-mode overrides, runner rebind, label edits.) Does **not** publish to WS
  updates (pull-based); does forward best-effort model/effort to the live runner.
- **Close** (sub-agent close) — not a dedicated route; set by `sys_session_close`. Gated by
  `is_session_closed(labels, title)` (`session_lifecycle.py:70`): true when label
  `omnigent.closed == "true"` **OR** the legacy title infix `:closed:`. `post_event` rejects
  new user input on a closed session (409, `:18306`); reads still allowed.
- **Delete** — `DELETE /v1/sessions/{id}` (`delete_session`, `:19358`), **owner-only**
  (admins bypass). Best-effort cleanup: runner-side `DELETE …/resources` (falls back to local
  terminal-registry cleanup if runner offline → delete still proceeds), session files +
  artifacts, optional worktree removal (`?delete_branch=true` → `git worktree remove --force`
  + `git branch -D` on the host), `delete_conversation`, cache evictions, and managed-host
  teardown (terminate sandbox + delete host row + revoke launch token) for `host_type=managed`.
  No SSE/WS publish on delete; watchers see a `removed` delta on the next 4 s tick.

---

## Per-harness notes (server side)

| Aspect | claude-sdk / codex (SDK) | claude-native / codex-native | polly / custom |
|---|---|---|---|
| User msg persist | server appends (I1), single writer | **bypass** — not persisted; transcript forwarder is writer; `pending_inputs` optimistic entry | same as claude-sdk |
| Durable output | relay persists `output_item.done` | forwarder POSTs `external_conversation_item` → server persists | same as claude-sdk |
| Status edges | runner `session.status` → relay → `_publish_status` | `external_session_status` → `_publish_status` | same as claude-sdk |
| Model/effort mid-session | `runner_body.model_override` next turn | best-effort: `external_model_change` persists column + `session.model` | same as claude-sdk |
| Interrupt | runner `executor.interrupt_session()` | bridge inject Escape (claude) / `turn/interrupt` RPC (codex) | same as claude-sdk |
| Policy | in-proc `_evaluate_input_policy` at POST /events | **also** the HTTP hook `evaluate_policy` (PreToolUse) → de-duped against in-flight web prompts (`:16065`) | same as claude-sdk |
| Live trace in corpus | **yes** (claude-sdk) | claude-native yes; **codex-native NO (creds expired)** | (run on claude-sdk) |

codex / codex-native are covered from code + CUJ-ANALYSIS §4 matrix + structural analogy to
claude (the server path is identical except the runner-side executor); **no live trace**
because the Databricks AI-gateway token is expired (403).

---

## Failure branches & gaps (server-owned)

- **Runner-offline-on-message:** persist OK, forward fails → `idle` published, runner resumes
  on reconnect; client bubble may sit until snapshot reconciliation (`:8705`).
- **No runner ever bound (fresh item):** 503 `RUNNER_UNAVAILABLE` **before** persist
  (`:19073`), unless host can relaunch (`:18920-19006`).
- **Hard runner affinity / no failover** (`routing.py:110-116`) — pinned-runner-offline
  strands the session.
- **`failed` sticky vs trailing idle** (`_publish_status` `:5389`) — intentional, native-driven.
- **`_session_status_cache` is per-process** (`:837`) — multi-replica sidebar may read a
  status another replica's relay wrote (open question).
- **Permissions disabled ⇒ `accessible_by=None` returns ALL sessions** — cross-user leak risk
  (CUJ-ANALYSIS §6 [§2.D]).
- **Native FIFO/pending-input desync** — server has no item id for native web messages, only
  `pending_id`; double-bubble risk if client dedup mis-orders (CUJ-ANALYSIS §6 [§2.A]).
- **`evaluate_policy` has no own span** — the `policy.evaluate` span is the engine's
  (`engine.py:255`), so in a trace the native hook = `POST …/policies/evaluate` FastAPI span
  with a `policy.evaluate` child.

## Open questions

1. Relay-restart window: can durable `output_item.done` events be missed during a
   `_relay_runner_stream` heartbeat-timeout flap before `_ensure_runner_relay_ready` respawns it?
2. Multi-replica: status/usage caches are in-memory — how is sidebar coherence maintained
   across replicas (sticky sessions? or accepted staleness)?
3. Native dedup: confirm `cleared_pending_id` + client FIFO is the *only* guard against a
   double user bubble on native (no server item id exists at dispatch).
4. `refresh_state=true` snapshot — exact set of runner overlays re-pulled vs AP-cache.
