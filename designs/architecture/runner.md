> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.
>
> **⚠️ CORRECTION (round-2 live-driving):** the "**hard affinity, NO failover**" claim below is **too strong**. Affinity (one conv ↔ one `runner_id`, no load-rebalancing) holds, but an offline runner on a **live host is recovered**: the **message path** (`POST /events`) relaunches it via `host.launch_runner` (`_launch_runner_on_host` `sessions.py:6035`, "host-relaunch optimism" `app.py:1590`) + **LWW rebind** (`replace_runner_id`), returning `{queued:true}` so the turn runs. `RUNNER_UNAVAILABLE` (503, `routing.py:175`) is raised **only** on the resource path (`GET /resources/*`, no relaunch) or when the **host itself** is dead (../ARCHITECTURE.md §10 R2).

# Component: RUNNER

The per-conversation worker process that hosts the harness/executor, owns MCP + tool
execution + system resources (shells/cwd/timers), and reaches the server **only** over an
outbound WebSocket reverse-tunnel. All `path:line` anchors below were opened and confirmed in
`/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`.

---

## 1. Role & boundaries

**Owns:**
- The runner ASGI app (one FastAPI app per runner process) — `omnigent/runner/app.py` (18,843 lines).
- Per-conversation turn lifecycle: ingest gate → buffer-vs-turn decision → background turn task or streaming SSE (`post_session_events`, app.py:14598).
- MCP server pool (stdio subprocesses), keyed by spec_hash, LRU-8 (`omnigent/runner/mcp_manager.py`).
- Tool **execution** for every tool category (MCP, `sys_os_*`, terminals, files, REST, subagent, async-inbox) — `omnigent/runner/tool_dispatch.py:3971 execute_tool`.
- Runner-side **fast-path** policy enforcement for `function`-type TOOL_CALL/TOOL_RESULT policies (`omnigent/runner/policy.py`).
- The runner side of the WS tunnel client (`omnigent/runner/transports/ws_tunnel/serve.py`).
- Session resources: terminals/tmux panes, cwd, timers (`resource_registry.py`), in-memory transcript cache.
- Credential minting for its own server callbacks + tunnel handshake (`_entry.py:_make_auth_token_factory`, `_RunnerDatabricksAuth`).

**Does NOT own:**
- Durable conversation state / item store — that's the **server** (`/v1/sessions/{id}/items`); the runner caches a copy but the server is source of truth.
- The runner↔conversation **binding** decision — the **server** writes `conversations.runner_id`; the runner only reads its own id from env/disk. Routing/affinity is a server concern (`omnigent/runner/routing.py` runs **in the server process**, not the runner — it imports the server's `ConversationStore` + `TunnelRegistry`).
- Policy authority for `label`/`prompt` types and ASK elicitation — those stay server-side (policy.py:14-28).
- LLM credential resolution for in-process SDK harnesses (that's the executor/harness layer; runner only mints runner↔server + tunnel creds).

> Naming caution: `omnigent/runner/routing.py` and `transport.py` (server-side `WSTunnelTransport`) physically live under `omnigent/runner/` but **execute in the server process** — they are the server's view of the tunnel. The runner-side tunnel code is `transports/ws_tunnel/serve.py`.

---

## 2. Key files & entrypoints (verified)

| Path:line | What |
|---|---|
| `runner/_entry.py:881 _run_tunnel_from_env` | Runner main: builds app, inits telemetry, spawns `serve_tunnel` task + idle monitor. |
| `runner/_entry.py:950` | `serve_tunnel(app, server_url, runner_id, auth_token, tunnel_token=binding_token, auth_token_factory, on_reconnect=catch_up_scan, on_activity=_mark_activity)`. |
| `runner/_entry.py:271 _make_auth_token_factory` | Token factory: stored OIDC (`omnigent login`, keyed by server_url) → Databricks SDK OAuth. |
| `runner/_entry.py:162 _RunnerDatabricksAuth.auth_flow` | httpx Auth: fresh Bearer **per HTTP request** to server; retry-once on 401 **or** 302→`/oidc/`,`/.auth/`. |
| `runner/identity.py:98 token_bound_runner_id` | `runner_token_{sha256("omnigent-runner:"+token)[:32]}` — deterministic runner id from binding token. |
| `runner/identity.py:75 get_stable_runner_id` | env `OMNIGENT_RUNNER_ID` override, else `~/.omnigent/runners/runner_id` (uuid4). |
| `runner/app.py:14598 post_session_events` | `POST /v1/sessions/{conv}/events` — the turn entrypoint (server→runner over tunnel). |
| `runner/app.py:17829 mcp_execute` | `POST /v1/sessions/{id}/mcp/execute` — `tools/list` + `tools/call` execution (server→runner). |
| `runner/app.py:18399 _catch_up_scan` (assigned `app.state.catch_up_scan` :18478) | reconnect catch-up. |
| `runner/app.py:9834 _load_history_as_input` | paginated `GET …/items` → Responses-input shape (resume/cold-cache rehydrate). |
| `runner/transports/ws_tunnel/serve.py:230 serve_tunnel` | runner WS client loop + reconnect/auth-refresh. |
| `runner/transports/ws_tunnel/serve.py:102 dispatch_via_asgi` | frames a `request` → runner ASGI → `response.head`/`body`/`end`. |
| `runner/transports/ws_tunnel/registry.py:195 TunnelRegistry` | **server-side** runner-session registry + per-req reassembly (newest-wins). |
| `server/routes/runner_tunnel.py:277 tunnel` | **server-side** WS endpoint accepting the runner's outbound tunnel. |
| `runner/transports/ws_tunnel/transport.py:102 WSTunnelTransport` | **server-side** httpx transport that rides the tunnel. |
| `runner/routing.py:68 RunnerRouter` | **server-side** conversation→runner dispatch (hard affinity, no failover). |
| `stores/conversation_store/sqlalchemy_store.py:1918 set_runner_id` | initial bind **CAS** (`WHERE runner_id IS NULL`); called `sessions.py:13914` (create). |
| `…sqlalchemy_store.py:1951 replace_runner_id` | **LWW** rebind; called `sessions.py:6067` (re-launch), `:14982` (PATCH/resume), `:5253` (subagent heal). |
| `server/routes/sessions.py:13056 _handle_mcp_tools_list` / `:13135 _handle_mcp_tools_call` / `:20050 mcp_proxy` | server-side MCP proxy → forwards to runner `…/mcp/execute`. |

---

## 3. Internal model

**Runner process = one ASGI app, many conversations.** State lives in module-level dicts (app.py ~7826–7885), keyed by `session_id`/`conversation_id`. Notable in-memory caches (all per-runner, lost on runner death → rebuilt from server on next turn):

- `_session_histories` — transcript as harness-input items (source of resume + catch-up). Built by `_load_history_as_input` from server `/items`.
- `_last_server_item_id` — cursor for incremental catch-up.
- `_active_turns: dict[conv, Task|None]` — single-active-turn invariant (I2); `None` = reserved-but-not-yet-started.
- `_session_message_buffers` — messages buffered while a turn is active (drained as post-turn continuation).
- `_ingest_next_seq` / `_ingest_now_serving` / `_ingest_cond` — per-conv FIFO ingest gate (arrival-order serialization).
- `_live_response_id` — conv→live turn's harness `response_id`; gates the mid-turn injection forward.
- `_session_spec_cache` / `_session_snapshot_cache` (+ per-session locks) — one `GET /v1/sessions/{id}` projected into agent_id/workspace/created_at; locked so a startup read-burst shares one fetch.
- `_session_skills_cache` (TTL `_SESSION_SKILLS_CACHE_TTL_SECONDS`) — runner-filesystem skill walk, re-runs at most once/TTL so mid-session installs surface.
- `_session_tool_schemas` / `_session_mcp_spec_hash`, `_session_agent_ids`, `_session_sub_agent_names`, `_session_advisor_applied_model`, per-harness `_*_terminal_ensure_locks`.

**MCP pool** (`RunnerMcpManager`, mcp_manager.py:150): `dict[spec_hash → _SpecEntry{servers, prewarm_task}]`, LRU list cap `_POOL_SPEC_CAPACITY=8` (mcp_manager.py:45). `spec_hash = sha256(servers + stdio_cwd)[:16]` (mcp_manager.py:74). Tools namespaced `{server}__{tool}` (mcp_manager.py:141). Prewarmed on session start; connections are live stdio subprocesses shared across conversations using the same spec.

**Spec cache (agent bundles):** `_spec_cache_root = tempfile.mkdtemp("runner-specs-{runner_id}-")` (`_entry.py:758`); bundles fetched + extracted per `agent_id/version` (`_agent_cache_dest`, path-traversal-guarded `_entry.py:561`).

**Identity:** stable `runner_id` from env or `~/.omnigent/runners/runner_id`; on a shared server it's `token_bound_runner_id(binding_token)` (deterministic SHA-256). Binding token = control-plane secret, stripped at every child-spawn boundary (`identity.py:58 strip_runner_auth_secrets`).

---

## 4. Inter-component channels

### 4a. The ONLY production server↔runner transport: the WS reverse-tunnel
`transports/__init__.py:1-17` is explicit — UDS/TCP are **test/future-shape only**; live traffic is the WS tunnel. The runner is the WS **client** (dials out, NAT-friendly); the server hosts `WS /v1/runners/{runner_id}/tunnel` (`runner_tunnel.py:277`).

```
RUNNER (ws client)                          SERVER (ws endpoint + registry)
serve.py:serve_tunnel ── websockets.connect ──▶ runner_tunnel.py:tunnel
   _send_hello (HelloFrame) ─────────────────▶ register() in TunnelRegistry (newest-wins)
                                                ── start _sender_loop/_ping_loop/_receive_loop
   ◀── RequestFrame (server→runner HTTP) ───────  WSTunnelTransport.handle_async_request
   dispatch_via_asgi → app(scope) ──────────▶
   ResponseHeadFrame/BodyFrame*/EndFrame ─────▶ route_response_frame → per-req reassembly queue
   ◀── PingFrame (30s) / PongFrame ───────────  _ping_loop (PING_INTERVAL_S=30, miss×3 ⇒ close 4003)
```

- **How server→runner HTTP rides the tunnel:** server code holds an `httpx.AsyncClient(transport=WSTunnelTransport(registry, runner_id), base_url="http://runner")` (routing.py:255). Each request → `RequestFrame{id,method,path,query_string,headers,body}` (transport.py:141). Runner rebuilds an ASGI `scope` and calls its own FastAPI app (serve.py:117). Response framed back as `response.head` + N×`response.body` + `response.end` (serve.py:156-197). Reassembly: head→`head_future`, body→`body_queue`, end→sentinel (registry.py:730-744).
- **Header forwarding is verbatim** (transport.py:149 `headers=[[k,v] for k,v in request.headers.items()]`; runner re-applies them lowercased into the ASGI scope, serve.py:126). Original traceparent/auth survive the hop. `RunnerRouter._client_for_runner` instruments the per-runner client directly (routing.py:264) because the global httpx instrumentation can't see the custom transport.
- **Streaming/SSE over the tunnel:** runner `receive()` deliberately **never synthesizes `http.disconnect`** after the body (serve.py:137-154) — otherwise Starlette's `StreamingResponse` would cancel the SSE stream early. `RequestFrame.stream=True` is an advisory hint. A real disconnect or a `RequestCancelFrame` (sent on consumer early-exit, transport.py:84) cancels the runner dispatch task.
- **Browser terminal attach** is multiplexed on the SAME tunnel as WS *channels* (`ws.open`/`ws.frame`/`ws.close`) — `server/_runner_ws_tunnel.py` (server side) + `serve.py:_dispatch_ws_via_asgi` (runner side). Keystrokes ride as base64 `WSFrame`, resize as utf-8.
- **Keepalive:** server pings every 30s; declares the runner dead after 3 missed intervals (`runner_tunnel.py:43,629-663`). Runner answers `PongFrame` and does NOT count pongs as activity for its idle watchdog (`on_activity` fires only on real work frames, serve.py:629/_entry.py:917).
- **Max frame size:** `RUNNER_TUNNEL_MAX_MESSAGE_BYTES` (`limits.py`), set on `websockets.connect(max_size=…)` (serve.py:547).

### 4b. Runner → server callbacks (HTTP, riding the SAME tunnel back; auth = `_RunnerDatabricksAuth`)
Confirmed against trace corpus `conv_fc47380…` (subagent) and `conv_32db3f59…` (sdk-tools), edge `omni-runner → omni-server`:

| Edge (corpus) | Code | Purpose |
|---|---|---|
| `GET …/items` | app.py:9834 `_load_history_as_input`, :18424 catch-up | transcript load / catch-up pagination |
| `GET …/agent/contents` | spec/bundle + agent contents fetch | resolve agent spec |
| `POST …/policies/evaluate` | tool_dispatch.py:5336 (sub-agent TOOL_RESULT), SDK-path ASK-escalation | server-side policy authority |
| `POST …/mcp` | runner-side proxy_mcp_manager (server-routed MCP) | namespaced MCP schemas/exec via server |
| `GET …/{id}` | `_session_snapshot_cache` fill | session snapshot (agent_id, workspace, created_at) |
| `PATCH …/{id}` | runner re-asserts/clears its `runner_id` binding | bind/rebind |
| `GET /api/version` | version-skew check at startup | — |
| `GET …/child_sessions` | subagent enumeration | — |
| `POST …/{id}/events` (to **server**) | turn-output relay / status | emit events back |

### 4c. Server → runner (HTTP over tunnel) — corpus `omni-server → omni-runner`
`POST /v1/sessions` (create/start), `GET /v1/sessions/{id}` (probe), `POST …/events` (turn drive + control events), `POST …/mcp/execute` (tool exec), `GET …/resources/terminals`, `GET …/skills`. All dispatched via `RunnerRouter.client_for_session_resources`/`client_for_conversation` (routing.py:89/118) → `WSTunnelTransport`.

### 4d. Runner → harness (in-process or subprocess; HTTP)
`POST /v1/sessions/{conv}/events` to the harness process — corpus edge `omni-runner → omni-harness` ×10 (subagent). The runner is the harness's controller: it forwards the resolved message (`process_manager.get_client(conv).post(…)`, app.py:14780) for mid-turn injection, and consumes the harness SSE for the streaming turn (`_stream_message_to_harness`, app.py:14892). For **native** harnesses the "harness" is a TUI in a tmux pane; control is via key sends, not HTTP (interrupt routing app.py:14928-14958).

---

## 5. CUJ behaviors (runner's slice)

### Request lifecycle (forwarded input → executor → output back)
`POST /v1/sessions/{conv}/events` (app.py:14598):
1. **FIFO ingest gate** (app.py:14692-14700): read-increment `_ingest_next_seq` synchronously *before any await*, then wait on `_ingest_cond` until `_ingest_now_serving == my_seq`. Guarantees arrival-order across content-resolution latency (the "first-message FIFO desync" fix, PR #1457). `finally` advances the gate even on error (app.py:14924).
2. **Content resolution** (`_resolve_forwarded_message_content`, app.py:14704) — resolves `file_id`→`image_url`/`file_data` blocks.
3. **Buffer-vs-turn decision** (app.py:14711): if `conv in _active_turns`, buffer (`_session_message_buffers`). Forward as a **live injection** only if non-native **and** not awaiting-approval **and** `conv in _live_response_id` (app.py:14741). Native: never forward (instant turns race teardown) — always drained via post-turn continuation (app.py:14766). Awaiting-approval: buffer-only, no forward (can't steer a human-gated turn, app.py:14718-14726).
4. **Start turn** (app.py:14860): `_active_turns[conv]=None`, `_publish_turn_status(conv,"running")`. Cold cache → rehydrate via `_load_history_as_input` (drop just-persisted pre-resolution item, append resolved, app.py:14852-14858).
5. **Output back, two modes:**
   - `stream=true` → `StreamingResponse` whose **body IS the SSE** (app.py:14892); harness `response.created`→tool-dispatch→pairing flow consumed inline.
   - `stream=false` → background `_run_turn_bg` task, **202 Accepted**; events flow out via `GET /v1/sessions/{conv}/stream` (app.py:14903). Corpus shows both `GET …/stream` ×2 and the 202 path.

### Disconnect / reconnect (runner side)
- **Runner is WS client**; on any drop `serve_tunnel` retries forever: 0.5s→10s cap, ±50% jitter (serve.py:74-76). Backoff escalates only on non-recycle failures.
- **Routine ingress recycles** (close 1001/1012, HTTP 502 — Databricks Apps cycling long-lived WS) reset backoff to base, no escalation (serve.py:87,343-353) — keeps the runner registered.
- **Fatal:** close 4001/4002/4004/4500 or HTTP 403 → `RuntimeError` (give up). 401 → refresh token once and retry (serve.py:326). InvalidURI redirect to http(s) (Apps OAuth login) → fail loud with "run `omnigent setup`" (serve.py:309-324).
- **On reconnect** (`on_reconnect=catch_up_scan`, app.py:18399): for each session with in-memory history, paginate `GET …/items` after `_last_server_item_id`, append new items, and **auto-start a turn** if idle + last new item is a `user` message (app.py:18449). ⚠️ **Native harnesses are skipped** (app.py:18408) — they don't replay mirrored transcript items.
- **Server side on tunnel close** (`registry.deregister`, registry.py:299): aborts all in-flight requests with `ConnectionError` so awaiters fail fast rather than hang; fires `on_runner_disconnect` (server marks sessions `runner_offline`).

### Resume (how much transcript loads into the runner?)
**The full transcript.** `_load_history_as_input` (app.py:9834) paginates `GET /v1/sessions/{id}/items?limit=100&order=asc&after=…` until `has_more=false`, converts every item to Responses-input shape, caches in `_session_histories`. There is no windowing here; compaction is applied upstream by the server's item store, not the runner.

### Fork / new-runner resume
A resumed session (possibly on a **new** runner after relaunch) gets a fresh `runner_id`; the server rebinds via `PATCH`/host-launch (`replace_runner_id`). The runner rehydrates history lazily on first turn. ⚠️ Native **sub-agent** children copy the parent's `runner_id` once at creation (sessions.py:5195-5196) and can point at a dead runner after relaunch — healed via parent on forward (sessions.py:5209-5253, PR #1446).

### Policy enforcement (two distinct paths — important)
- **SDK/proxy path (claude-sdk, codex):** runner enforces `function` TOOL_CALL/TOOL_RESULT policies **locally** via `RunnerToolPolicyGate` (policy.py). DENY → refusal text fed back as tool output (loud-fail). ASK → runner escalates by `POST …/policies/evaluate` to the server, which parks an elicitation; runner awaits via `pending_approvals` (policy.py:20-28). `__agent_start` reuses the same gate (app.py:18774-18800). `label`/`prompt` policies always stay server-side.
- **Server-routed MCP path:** server's `POST /v1/sessions/{id}/mcp` evaluates TOOL_CALL/TOOL_RESULT centrally, **then** forwards `tools/call`/`tools/list` to runner's `POST …/mcp/execute` (app.py:17829) over the tunnel. The runner-side `/mcp/execute` does NOT re-run policy — execution only (tool_dispatch.py:4022-4025 comment: "routed through the AP server's /mcp endpoint … No runner-side policy gate needed"). Corpus `conv_fc47380…` shows `server→runner POST …/mcp/execute ×3` + `runner→server POST …/mcp ×3`.

### MCP routing (custom + Omnigent MCP)
- Custom MCP servers: `RunnerMcpManager` spawns/holds stdio subprocess connections, prewarmed per spec_hash, namespaced `server__tool` (mcp_manager.py).
- Omnigent system tools (`sys_os_*`, `sys_terminal_*`, `sys_session_*`, etc.): runner-local, dispatched by `tool_dispatch.execute_tool` (categories at app.py via `_OS_ENV_TOOLS`/`_TERMINAL_TOOLS`/…).
- Inline MCP elicitation: runner POSTs `{type:"mcp_elicitation"}` to `…/events`, parks on `pending_approvals`; on no server client, declines (mcp_manager.py:182-277).

### System resources (shells, cwd, timers)
Runner-owned via `resource_registry.py`: terminals are tmux panes launched by the runner (`sys_terminal_launch`), shared cwd from `RUNNER_WORKSPACE`/session-isolation (`OMNIGENT_RUNNER_ISOLATE_SESSION`), timers in `_session_timers`. Browser attach proxies the pane over the tunnel WS channel (4a).

### Close page & idle
Idle watchdog (`_run_inactivity_monitor`, default 1h `_DEFAULT_RUNNER_IDLE_TIMEOUT_S`) shuts the runner down when no real activity AND no active work (`_entry.py:909,935-943,965`). Tmux-detach "adopt" (SIGUSR1) makes the parent-death killer stand down so the runner outlives the CLI (identity.py:14-19).

---

## 6. Answers to the doc questions (runner area)

- **Runner dispatch / affinity — HARD affinity, NO failover: CONFIRMED.** `RunnerRouter.client_for_conversation` (routing.py:89) reads `conversations.runner_id`; if set, validates the bound runner is online + supports the harness, else raises `RUNNER_UNAVAILABLE`/`CONFLICT`/`RUNNER_CAPABILITY_MISMATCH` (routing.py:107-116, 220-241). It **never selects a different runner**. There is no round-robin/least-loaded fallback for a bound conversation. `online_runner_ids()` round-robin (registry.py:456) exists but is only for listing, not for re-binding a pinned conversation.
- **The "binding CAS" — there ARE two store methods, one real CAS + one LWW:**
  - **Initial pin = real compare-and-set.** `conversation_store.set_runner_id` (`sqlalchemy_store.py:1918-1949`) is `UPDATE … WHERE id=? AND runner_id IS NULL`, returning `rowcount==1`. Concurrent first-dispatches racing to pin the same NULL conversation are DB-serialized — exactly one wins, the loser gets `False` and re-reads the winner. Used for NEW conversations.
  - **Rebind = unconditional last-write-wins.** `conversation_store.replace_runner_id` (`sqlalchemy_store.py:1951-1975`: `row.runner_id = runner_id`) has no guard. Used for resume / on-demand host re-launch (`sessions.py:~6064-6067` mints `binding_token`→`token_bound_runner_id`→`replace_runner_id`) and the native-subagent heal (`sessions.py:5253`).
  - Separately, the **tunnel registry** has its own "newest-wins" (registry.py:280-297): a second WS connect for the same `runner_id` discards the old session + aborts its in-flight — tunnel-session replacement, NOT the DB binding.
- **WS reverse-tunnel mechanics:** see §4a. Runner = client; server→runner HTTP is framed (`RequestFrame`→ASGI→`ResponseHead/Body/End`); headers forwarded verbatim; 30s server ping / 3-miss death; per-`req_id` reassembly with cross-loop `call_soon_threadsafe` wakeups; generation guards (session identity) on every registry mutation so a stale handler can't poison a newer tunnel.
- **Runner↔server auth + refresh — CONFIRMED:**
  - **`_make_auth_token_factory`** (`_entry.py:271`): resolves stored OIDC token (per server_url, from `omnigent login`) first, else Databricks SDK OAuth (reused `Config`, served from SDK in-memory cache; CLI re-shell only near expiry — the perf fix at _entry.py:312-323). Returns `None` when no creds.
  - **Tunnel (WS) refresh = per-RECONNECT, not per-message.** `serve_tunnel` calls `_refresh_auth_token(factory)` before *each* `_serve_tunnel_once` (serve.py:284) and once more on a 401 handshake rejection (serve.py:326-331, `_handle_refreshable_auth_failure`). The Bearer is set in the WS handshake headers only (serve.py:540); there is no per-frame auth.
  - **HTTP callbacks refresh = per-REQUEST.** `_RunnerDatabricksAuth.auth_flow` (_entry.py:192) mints a fresh Bearer on every runner→server request and retries once on 401 **or** a 302→`/oidc/`/`/.auth/` Apps-login redirect (the Apps front door 302s instead of 401 — _entry.py:204-211, 241-268). Fails closed: configured factory returning no token raises rather than sending unauthenticated.
- **What the runner caches:** see §3 — transcript (`_session_histories`), session snapshot/spec/skills/tool-schemas, MCP connections (LRU-8 by spec_hash), agent bundles (tempdir), advisor-applied model, per-harness ensure-locks. Refresh cadence: history via catch-up cursor on reconnect + cold-rehydrate per turn; skills via TTL; snapshot/spec lazily (lock-shared); MCP on spec change / prewarm; auth tokens via the two factories above.

---

## 7. Reliability gaps / sharp edges (confirmable in code)

1. **Hard affinity + no failover** (routing.py:107-116): if the bound runner is offline and never reconnects (e.g. host died, no relaunch), every dispatch raises `RUNNER_UNAVAILABLE`. Recovery depends entirely on the host relaunching the runner under the *same* `runner_id` (token-bound) or the server rebinding. No automatic migration of a live conversation to another runner.
2. **Native sub-agent stale `runner_id`** (sessions.py:5195-5253): child copies parent's `runner_id` at creation; after a parent-runner relaunch the child points at a dead runner. Mitigated by heal-via-parent on forward, but the child is broken until a forward triggers the heal.
3. **Catch-up auto-turn only for non-native** (app.py:18408,18449): a native session that received a user message during a disconnect won't get a synthesized catch-up turn; delivery depends on the native pane's own state. Non-native sessions auto-start a turn purely from "last item is user" — a benign duplicate user item could in principle start an unwanted turn.
4. **Rebind path (`replace_runner_id`) is LWW with no guard** (sqlalchemy_store.py:1973), unlike the initial pin which is a proper CAS (`set_runner_id` :1918). So two concurrent *rebinders* (e.g. `_on_runner_connect` + message-path relaunch handshake — the race the `_claude_terminal_ensure_locks` comment at app.py:7877-7884 calls out) can stomp each other's `runner_id`. The per-session ensure-locks guard terminal double-launch but not the DB rebind itself.
5. **In-flight requests die on tunnel drop** (registry.py:334-337): mid-request server→runner calls get `ConnectionError`. For a streaming turn this surfaces as an aborted SSE; idempotency/retry is the caller's problem.
6. **`HelloFrame.harnesses` is a hardcoded list** (serve.py:581-592): `claude-native, claude-sdk, codex, openai-agents, open-responses, pi`. ⚠️ It does **not** include `codex-native` (nor several others the runner actually serves: hermes/cursor/goose/kiro/qwen/kimi native, per app.py interrupt handlers). `_runner_supports_harness` (routing.py:269) gates dispatch on this list, so a conversation whose spec resolves to a harness absent from the hello frame would be rejected with `RUNNER_CAPABILITY_MISMATCH` even though the runner can run it. (Flagged for the stitcher — confirm whether `codex-native`/other native kinds canonicalize to one of the listed keys; `_EXECUTOR_TYPE_TO_HARNESS` only maps `claude_sdk`.)
7. **Idle watchdog vs adopted runner** (_entry.py:909): idle timeout still fires for a detached/adopted runner serving only the web UI if it sees no "activity" frames for an hour, even though a user might return — the 1h budget is the only knob.

---

## 8. Corrections to CUJ-ANALYSIS

Per the briefing, I treated CUJ-ANALYSIS §2.F/§2.G as hypotheses. I could not open `designs/CUJ-ANALYSIS.md` in this pass (not located under the worktree root via the read tools I used), so these are **corrections framed against the briefing's stated claims** for §2.F (runner dispatch) / §2.G (runner↔server auth) — flag any that the actual doc already states correctly:

1. **"binding CAS" applies only to the INITIAL pin, not rebind (§2.F).** There are two distinct store methods: `set_runner_id` is a real CAS (`UPDATE … WHERE runner_id IS NULL`, sqlalchemy_store.py:1918-1949) used for new conversations; `replace_runner_id` is unconditional LWW (sqlalchemy_store.py:1951-1975) used for resume / host re-launch / subagent heal. Any claim that binding is *always* CAS, or *always* LWW, is half-wrong. Separately, the **tunnel-session newest-wins** in `TunnelRegistry.register` (registry.py:280-297) is a different object (live WS session, not the DB row) — do not conflate it with either DB method.
2. **Auth refresh is two different cadences, not one (§2.G).** Tunnel (WS) Bearer refreshes **per reconnect** (`serve.py:284` + 401 retry); HTTP callbacks refresh **per request** with a 401-**or**-302-to-`/oidc/` retry (`_RunnerDatabricksAuth.auth_flow`, _entry.py:192-238). Any claim that "the tunnel refreshes tokens per message" or that "401 is the only re-auth trigger" is wrong — the Apps front door 302s instead of 401, which is why the redirect branch exists.
3. **`routing.py`/`transport.py` run in the SERVER, not the runner (§2.F).** Despite living under `omnigent/runner/`, `RunnerRouter` + `WSTunnelTransport` execute in the server process (they import the server's `ConversationStore`/`TunnelRegistry`). The runner-side tunnel code is `transports/ws_tunnel/serve.py`. Any anchor attributing dispatch/affinity logic to "the runner process" is mislocated.

(Trace evidence: `conv_fc47380ccbff481abf452a446ec4e40d` and `conv_32db3f5927d9459fa028cbe69d4173d3` summaries — every runner↔server edge enumerated in §4b/§4c was matched to a handler above.)
