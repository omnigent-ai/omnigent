# Omnigent Runner — Architecture

**Scope:** `omnigent/runner/` (`_entry.py`, `app.py`, `routing.py`, `mcp_manager.py`,
`proxy_mcp_manager.py`, `policy.py`, `tool_dispatch.py`), the runner↔server reverse WS
tunnel (`runner/transports/ws_tunnel/` + server `routes/runner_tunnel.py`) **from the
runner side**, and the harness/executor subprocess boundary (`runtime/harnesses/process_manager.py`, UDS).

> Source-of-truth: code (cited `file:line`), validated against live Jaeger traces from
> `conv_63542a5f92e24956812e19b104eac0e9` (claude-sdk tool-use). Doc claims from
> `designs/OBSERVABILITY.md` / `RUNNER*.md` are tagged when only doc-sourced.

---

## 1. Overview

The **runner** is a standalone ASGI (FastAPI) process that owns *execution*: it spawns and
supervises harness/executor subprocesses, owns custom-MCP stdio subprocesses, owns terminals
and the OS sandbox, and runs the agent turn loop. It is **not** directly reachable over the
network. Instead it dials *out* to the server and opens a **reverse WebSocket tunnel**; the
server then makes ordinary HTTP requests *into* the runner by framing them over that tunnel
(`WSTunnelTransport`). The runner is the WS **client**; the server is the WS **server**.

Two distinct mechanisms route between server and runner over the **same** tunnel:
- **server → runner** (turn dispatch, `/mcp/execute`, resource APIs): the server holds an
  `httpx.AsyncClient` whose transport is `WSTunnelTransport` (`routing.py:255`). Each httpx
  request becomes a `request` frame; the runner replays it through its ASGI app
  (`serve.py:dispatch_via_asgi`).
- **runner → server** (load transcript/spec, escalate policy ASK, `external_*` mirror,
  `/mcp` proxy): the runner holds its own `httpx.AsyncClient` (`_entry.py:718`) pointed at
  the real server URL, authed with `_RunnerDatabricksAuth`.

The runner advertises which **harnesses** it can serve in its hello frame; the server binds a
conversation to exactly one runner (hard affinity, no failover) and validates the runner is
online + capable before every dispatch.

### Process entrypoint (`runner/_entry.py`)
`_run_tunnel_from_env()` (`_entry.py:881`) is the runner process main:
1. Resolve `server_url`, build the auth-token factory (`_make_auth_token_factory`, `:271`),
   read the binding token + parent pid.
2. `telemetry.init("omni-runner")` (`:902`) — sets `service.name=omni-runner`; no-op unless
   `OMNIGENT_TELEMETRY_ENABLED` + `OTEL_EXPORTER_OTLP_ENDPOINT`.
3. `create_app(auth_token_factory=...)` (`:908`, → `create_app` `:660`) — builds the FastAPI
   app, `HarnessProcessManager`, `RunnerMcpManager`, terminal registry, server httpx client.
4. Drive the app lifespan (`pm.start()` + MCP prewarm + native-pane reaper).
5. `serve_tunnel(app, …, auth_token_factory=…, on_reconnect=catch_up_scan, on_activity=…)`
   (`:949`) — the forever-reconnecting WS tunnel client.
6. Watchdogs: optional idle monitor (`_run_inactivity_monitor`), parent-death killer thread.

---

## 2. Key files (file:line)

| File | Role |
|---|---|
| `runner/_entry.py:881` | `_run_tunnel_from_env` — process main; telemetry init, lifespan, tunnel start |
| `runner/_entry.py:271` | `_make_auth_token_factory` — runner↔server Bearer factory (OIDC token → Databricks OAuth) |
| `runner/_entry.py:162` | `_RunnerDatabricksAuth` — per-request httpx auth (refresh per request, retry on 401/302) |
| `runner/_entry.py:660` | `create_app` — runner FastAPI factory; wires `server_client`, `RunnerMcpManager`, `pm` |
| `runner/app.py:14598` | `POST /v1/sessions/{conv}/events` — **inbound forwarded user event**; per-conv FIFO ingest gate; turn dispatch |
| `runner/app.py:9589` | `GET /v1/sessions/{conv}/stream` — SSE event stream back to server (202 fire-and-forget path) |
| `runner/app.py:17829` | `POST /v1/sessions/{conv}/mcp/execute` — server calls back to execute a tool (after server-side policy) |
| `runner/app.py:8654` | `POST /v1/sessions` — create session/turn on this runner |
| `runner/routing.py:89` | `RunnerRouter.client_for_conversation` — **dispatch + hard affinity** decision |
| `runner/routing.py:243` | `_client_for_runner` — cached tunnel httpx client; `instrument_httpx_client` on it |
| `runner/transports/ws_tunnel/transport.py:118` | `WSTunnelTransport.handle_async_request` — server-side; headers forwarded verbatim (`:149`) |
| `runner/transports/ws_tunnel/serve.py:230` | `serve_tunnel` — runner-side tunnel client (reconnect, auth refresh) |
| `runner/transports/ws_tunnel/serve.py:566` | `_send_hello` — advertises harness capabilities + frame proto version |
| `runner/transports/ws_tunnel/registry.py:242` | `TunnelRegistry.register` — "newest wins"; aborts old in-flight |
| `runner/mcp_manager.py:150` | `RunnerMcpManager` — direct custom-MCP pool (8-entry LRU, namespaced tools) |
| `runner/proxy_mcp_manager.py:42` | `ProxyMcpManager` — route ALL MCP through server `/mcp` (deployed mode) |
| `runner/policy.py:109` | `RunnerToolPolicyGate` — runner fast-path ALLOW/DENY/ASK for function policies |
| `runner/app.py:6248` | `_evaluate_policy_via_omnigent` — proxy harness `policy_evaluation.requested` → server `/policies/evaluate`, deliver `policy_verdict` back |
| `runner/app.py:14115` | `proxy_stream` — SSE relay from harness; intercepts `action_required`, accumulates `_session_histories`, `_publish_event` |
| `runner/app.py:12519` | `_on_proxy_stream_end` — turn finalize: pop `_active_turns`, publish `session.status` idle/failed |
| `server/routes/runner_tunnel.py:277` | server-side tunnel WS handler — token gate, owner resolution, register |
| `stores/conversation_store/sqlalchemy_store.py:1918` | `set_runner_id` — atomic CAS bind (`WHERE runner_id IS NULL`) |
| `runtime/harnesses/process_manager.py` | per-conversation harness subprocess over **UDS** (`/tmp/omnigent`), per-spawn bearer |

---

## 3. Data flow

### 3.1 Request lifecycle (one user turn, deployed mode)

```mermaid
sequenceDiagram
    participant C as Client (TUI/Web)
    participant S as omni-server
    participant R as omni-runner
    participant H as omni-harness (executor, UDS)
    participant M as Custom MCP / sys_* tool

    C->>S: POST /v1/sessions/{id}/events (message)
    Note over S: persist-before-forward; policy.evaluate (REQUEST)
    S->>R: POST /v1/sessions/{id}/events  (httpx over WS tunnel, stream=true)
    Note over R: FIFO ingest gate (_ingest_now_serving); single-active-turn (I2)
    R->>S: GET /v1/sessions/{id}/items     (load transcript)
    R->>S: GET /v1/sessions/{id}/agent/contents  (load agent spec bundle)
    R->>H: POST /v1/sessions/{id}/events   (executor, UDS) — SSE turn events
    H-->>R: TextChunk / ToolCallRequest / TurnComplete (SSE)
    Note over R: tool call detected
    R->>S: POST /v1/sessions/{id}/mcp (tools/call)  [ProxyMcpManager]
    Note over S: policy.evaluate (TOOL_CALL)
    S->>R: POST /v1/sessions/{id}/mcp/execute       (server delegates execution)
    R->>M: dispatch (RunnerMcpManager custom-MCP / execute_tool sys_*)
    M-->>R: tool output
    R-->>S: result (over /mcp/execute response)
    Note over S: policy.evaluate (TOOL_RESULT)
    S-->>R: tool result (over /mcp response)
    R-->>H: tool result -> harness continues
    R-->>S: response.output_text.delta / final item (SSE on the events stream)
    S-->>C: SSE response.* / session.*
```

Every edge in this diagram is **observed in the live trace** (see §5).

### 3.2 Tunnel establishment & dispatch decision

```mermaid
flowchart TD
    A[runner _entry: serve_tunnel] -->|WS connect /v1/runners/{id}/tunnel<br/>Origin=omnigent://internal<br/>Bearer + X-Databricks-Org-Id + tunnel_token| B[server runner_tunnel.tunnel]
    B -->|token gate + owner resolve<br/>fail-closed if unauth non-loopback| C{accept?}
    C -->|no| X[ws.close 4004 / 4001 / 4002]
    C -->|yes| D[recv hello frame: harnesses[], frame_proto_version]
    D -->|strict-major check| E[registry.register newest-wins]
    E --> F[runner online in TunnelRegistry]
    G[server dispatch: RunnerRouter.client_for_conversation] --> H{conv.runner_id set?}
    H -->|no| I[CONFLICT 'not bound']
    H -->|yes| J{registry.get online?}
    J -->|no| K[RUNNER_UNAVAILABLE]
    J -->|yes| L{hello.harnesses ∋ harness?}
    L -->|no| M[RUNNER_CAPABILITY_MISMATCH]
    L -->|yes| N[RoutedRunner: cached WSTunnelTransport client]
```

---

## 4. Channels & message/event types

### 4.1 Runner↔server reverse WS tunnel (`ws_tunnel/`)
- **Transport:** runner is WS client → `ws(s)://<server>/v1/runners/{runner_id}/tunnel`
  (`serve.py:_tunnel_url`, `:894`). Tunnel max message bytes capped (`limits.py`).
- **Frames** (`ws_tunnel/frames.py`): `hello`, `ping`/`pong`, `request`, `request.cancel`,
  `response.head`, `response.body`, `response.end`, plus WS-attach frames `ws.open`/`ws.frame`/
  `ws.close` (for tunneled terminal-attach websockets).
- **HTTP-over-WS (server→runner):** `WSTunnelTransport.handle_async_request` (`transport.py:118`)
  allocates a `req_id`, sends one `RequestFrame` (whole body inline — bodies are tiny JSON),
  awaits `head_future`, then streams `ResponseBodyFrame`s until `ResponseEndFrame`
  (`registry.RequestState`). **Headers forwarded verbatim** — `headers=[[k,v] for k,v in
  request.headers.items()]` (`transport.py:149`) — this is how `traceparent` crosses (§6).
- **ASGI replay (runner side):** `dispatch_via_asgi` (`serve.py:102`) builds an ASGI `http`
  scope from the request frame and calls the runner's FastAPI app directly (no TCP listener);
  `http.response.start`/`.body` map back to `response.head`/`.body`/`.end` frames.
  Notably `receive()` never synthesizes `http.disconnect` after the body (`serve.py:149`) so
  Starlette's `StreamingResponse` keeps proxying harness SSE chunks.
- **Hello frame capabilities (`serve.py:577`):** advertises
  `harnesses=[claude-native, claude-sdk, codex, openai-agents, open-responses, pi]` and
  `envs=[os_sandbox]`, `frame_protocol_version=1`.

### 4.2 Runner→server callbacks (`server_client`, `_entry.py:718`)
Ordinary httpx to the real server URL, `auth=_RunnerDatabricksAuth`, `Origin=omnigent://internal`
(passes the server's `require_trusted_origin` CSRF guard on multipart routes). Used for:
- `GET /v1/sessions/{id}/items` (transcript), `GET /v1/sessions/{id}/agent/contents` (spec bundle),
- `POST /v1/sessions/{id}/mcp` (ProxyMcpManager `tools/list` / `tools/call`),
- `POST /v1/sessions/{id}/policies/evaluate` (runner-side ASK escalation, via `pending_approvals`),
- `POST /v1/sessions/{id}/events` with `mcp_elicitation` (custom-MCP inline elicitation),
- `external_*` native mirror events, `GET /api/version`, file/upload APIs.

### 4.3 Runner→executor subprocess (UDS)
`HarnessProcessManager` (`process_manager.py`): **one subprocess per conversation**, lazily
spawned on first event, reachable over a per-conversation **Unix domain socket** under
`/tmp/omnigent` (`_socket_path`). The runner hands callers an `httpx.AsyncClient` pointed at
that UDS. Auth is a **per-spawn bearer token** for the harness control channel (defence-in-depth
on top of UDS uid-isolation). Idle reaper for abandoned subprocesses + crash detection.

### 4.4 Inbound event discriminated union (`app.py:14598`)
`POST /v1/sessions/{conv}/events` body is the harness union
(`runtime/harnesses/_scaffold.InboundEventRequest`):
- `message` (or absent type) → turn path: FIFO ingest gate → content resolution → turn-vs-buffer
  decision. `stream=true` ⇒ `StreamingResponse` (body IS the SSE stream); `stream=false` ⇒ 202 +
  events flow via `GET …/stream`.
- `interrupt` / `tool_result` / `approval` → forwarded **verbatim** to the harness as control events.

---

## 5. Trace evidence (live, `conv_63542a5f92e24956812e19b104eac0e9`)

Trace `cfb59197f6f9…` (events/turn, 3 services) shows the full lifecycle as a single connected
trace (filtered, db/connect spans removed):

```
omni-server POST /v1/sessions/{session_id}/events
  omni-server policy.evaluate                                  # REQUEST phase
  omni-server POST  ── (WS tunnel) ──>
    omni-runner POST /v1/sessions/{conversation_id}/events     # forwarded user event
      omni-runner GET ──> omni-server GET /v1/sessions/{id}/items        # load transcript
      omni-runner GET ──> omni-server GET /v1/sessions/{id}/agent/contents# load spec bundle
      omni-runner POST ──> omni-harness POST /v1/.../events     # executor over UDS (SSE)
      omni-runner POST ──> omni-server POST /v1/.../policies/evaluate     # runner ASK escalation
        omni-server policy.evaluate
      omni-runner POST ──> omni-server POST /v1/.../mcp          # ProxyMcpManager tools/call
        omni-server policy.evaluate                              # TOOL_CALL
        omni-server POST ──> omni-runner POST /v1/.../mcp/execute # server delegates execution
        omni-server policy.evaluate                              # TOOL_RESULT
      omni-runner POST ──> omni-harness POST /v1/.../events      # tool result back to harness
```

**Cross-service edges (from the conv summary):**
- `omni-server → omni-runner [POST /v1/sessions]` ×3 — turn create/dispatch over tunnel.
- `omni-server → omni-runner [POST /v1/sessions/{id}/mcp/execute]` — server delegates tool execution.
- `omni-server → omni-runner [GET /v1/sessions/{id}]`, `[GET …/resources/terminals]` — resource APIs over tunnel.
- `omni-runner → omni-server [GET /v1/sessions/{id}/items]` ×4, `[GET …/agent/contents]` ×4 — transcript + spec load.
- `omni-runner → omni-server [POST /v1/sessions/{id}/policies/evaluate]` ×2 — runner ASK escalation.
- `omni-runner → omni-server [POST /v1/sessions/{id}/mcp]` — ProxyMcpManager.
- `omni-runner → omni-harness [POST /v1/sessions/{conversation_id}/events]` ×4 — executor over UDS.
- `omni-runner → omni-server [PATCH /v1/sessions/{id}]`, `[GET /api/version]`.

**Tunnel itself:** the `omni-server HTTP /v1/runners/{runner_id}/tunnel websocket
send/receive` spans (123 receive / 36 send in this conv) are the WS frames; `omni-runner GET
/v1/sessions/{session_id}/stream http send` (67) is the runner streaming SSE chunks back.

**Propagation:** the runner spans nest under the server spans in ONE trace, confirming
`telemetry.instrument_httpx_client(client)` on the cached `WSTunnelTransport` client
(`routing.py:264`) injects `traceparent`, FastAPI on the runner app extracts it
(`telemetry.instrument_fastapi_app`), and the verbatim header forwarding (`transport.py:149`)
carries it across. Without that instance-level instrumentation the global `HTTPXClientInstrumentor`
would miss the custom transport and the runner would root a disconnected trace
(OBSERVABILITY.md §12 "custom-transport client gap — resolved").

---

## 6. Channel auth & trace-context crossing

### 6.1 Runner↔server tunnel handshake (`runner_tunnel.py:277`, `serve.py:494`)
On the runner side (`_serve_tunnel_once`, `serve.py:494`) the WS upgrade carries:
- `Origin: omnigent://internal` (first-party sentinel → passes server CSWSH/origin guard),
- `Authorization: Bearer <token>` + `X-Databricks-Org-Id` (from `databricks_request_headers`),
- `X-Omnigent-Runner-Tunnel-Token` binding token (`RUNNER_TUNNEL_TOKEN_HEADER`).

Server side (`tunnel`, `runner_tunnel.py:277`): (1) validate tunnel token / derive expected
runner_id (`_expected_runner_id_from_headers`); reject mismatch with close `4004` **before
accept**; (2) resolve owner (`_resolve_tunnel_owner`) — **fail-closed** for an unauthenticated
non-loopback peer (would otherwise register `owner=None` and bypass owner-scoped guards);
(3) `accept()`; (4) receive hello, **strict-major** `frame_protocol_version` check (close `4002`
on skew); (5) `registry.register(owner=…)`.

### 6.2 The Bearer-once-at-open refresh model (KNOWN GAP, partially mitigated)
**The Bearer is set once, in the WS upgrade headers** (`serve.py:540`). The long-lived WS then
carries every server→runner `request` frame with **no per-message re-auth** — there is no place
to re-inject a header on an already-open socket. Mitigation: `serve_tunnel` re-mints the token on
**every reconnect** via `auth_token_factory` (`serve.py:284` `_refresh_auth_token`) and, on a
handshake **HTTP 401**, refreshes once and retries (`serve.py:326`, `_handle_refreshable_auth_failure`).
Routine ingress recycles (close 1001/1012, HTTP 502) reset backoff and reconnect promptly
(`serve.py:343-353`) — which *also* re-mints. So in practice a token expiry surfaces only if the
socket stays open *past* expiry with no recycle; the next reconnect heals it. **This is the
runner↔server analog of the "no per-message refresh" item in CUJ-ANALYSIS §6/§2.G.**

The **runner→server** direction has no such gap: `_RunnerDatabricksAuth.auth_flow`
(`_entry.py:192`) mints a fresh token **per request** and retries once on 401 or Apps
`302→/oidc/` (`_is_login_redirect_or_unauthorized`, `:241`). One SDK `Config` is cached for the
factory's life so the OAuth token comes from the SDK's in-memory cache (no CLI shellout per
request) — `_entry.py:312-360`.

### 6.3 Trace context
- **server→runner:** `instrument_httpx_client` on the per-runner cached client (`routing.py:264`)
  injects `traceparent`; tunnel forwards headers verbatim (`transport.py:149`); runner FastAPI
  extracts. Continuous trace (verified §5).
- **runner→server callbacks:** covered by the process-wide `HTTPXClientInstrumentor`
  (`telemetry._instrument_httpx`) — standard transport, no special handling.
- **runner→executor (UDS):** standard httpx over UDS → process-wide instrumentor injects;
  harness FastAPI extracts. Verified: `omni-harness` spans nest under `omni-runner` in the trace.
- **Runner-app FastAPI instrumentation (the OBSERVABILITY §12 open item) is CONFIRMED:**
  `create_runner_app` calls `telemetry.instrument_fastapi_app(app)` right after building the app
  (`app.py:7775-7782`) — "so HTTP requests tunneled in from the server (whose headers are
  forwarded verbatim) continue the caller's trace instead of starting a new one." The runner's
  auth middleware exempts tunnel-originated requests (`scope client[0] == "tunnel"`) and `/health`.
- **Seed:** root trace-id derivable from `response_id` via `trace_id_from_response_id`
  (`telemetry.py:498`); `session.id=conv_…` tags every span as the cross-trace grouping key.

---

## 7. MCP routing (custom MCP vs `sys_*`) — runner's role

There are **two MCP managers with the same interface**, selected by deployment mode:

| Manager | When | Who enforces policy | Who holds MCP connections |
|---|---|---|---|
| `ProxyMcpManager` (`proxy_mcp_manager.py:42`) | **Deployed** (runner + server) | **Server** (`/mcp` → `_evaluate_tool_call_policy`) | **Server** `ServerMcpPool` + delegates exec to runner |
| `RunnerMcpManager` (`mcp_manager.py:150`) | **No-server / test** | **Runner** (`RunnerToolPolicyGate`) | **Runner** (direct stdio/HTTP, 8-entry LRU) |

### 7.1 The deployed loop (what the trace shows)
1. Harness emits a tool call. Runner's `ProxyMcpManager.call_tool` POSTs `tools/call` to
   server `/v1/sessions/{id}/mcp` (`proxy_mcp_manager.py:220`).
2. Server runs **TOOL_CALL** policy, then calls **back into the runner** via
   `POST /v1/sessions/{id}/mcp/execute` (`app.py:17829`). *The server owns policy + routing; the
   runner owns execution* (so tools run on the right machine with the right cwd/env).
3. **The routing key inside `/mcp/execute` is the `__` separator** (`app.py:17954`):
   - `"__" in tool_name` → **custom MCP** → `RunnerMcpManager` (live stdio/HTTP subprocess);
     the namespaced `{server}__{tool}` is validated against the server prefix, then stripped.
   - **no `__`** → **runner-local builtin** (`sys_os_*`, `sys_terminal_*`) → `execute_tool`
     (`tool_dispatch.py`) with the session's terminal registry / inbox / sandbox workspace.
4. Server runs **TOOL_RESULT** policy on the returned output, then returns the (possibly
   transformed/denied) result to the runner, which feeds it to the harness.

**Two ASK-escalation paths exist** (don't conflate):
- *Function-policy ASK in no-server `RunnerMcpManager` mode* — `RunnerToolPolicyGate` returns an
  ASK verdict (`policy.py:159`); `tool_dispatch.execute_tool` escalates by POSTing
  `evaluate_policy=True` to the server and awaiting via `pending_approvals` (`policy.py` module
  docstring).
- *Harness-driven ASK in deployed mode* — the harness emits a `policy_evaluation.requested` SSE
  event inside `proxy_stream`; the runner calls `_evaluate_policy_via_omnigent` (`app.py:6248`)
  which POSTs `/v1/sessions/{id}/policies/evaluate` and delivers the verdict back to the harness
  as a `policy_verdict` inbound event. **This is the path the live trace shows** (the
  `omni-runner → omni-server POST …/policies/evaluate` edges, each wrapping a server `policy.evaluate` span).

### 7.2 ASK / elicitation across the proxy (MRTR)
If the server's policy returns ASK, `/mcp` returns an `input_required` (MCP Multi-Round-Trip
Requests) result. `ProxyMcpManager.call_tool` parks on `pending_approvals.wait_for_user_approval`
(`proxy_mcp_manager.py:281`), then retries `tools/call` once with `inputResponses` +
`requestState` (`:287`). For a **custom MCP server** that itself elicits, the runner surfaces
`McpElicitationRequired` back through `/mcp/execute` (`app.py:18017`) so the server owns the
elicitation channel; the runner replays with `call_tool_with_elicitation` on retry (`:18004`).

### 7.3 `RunnerMcpManager` internals (direct / no-server mode)
- 8-entry **LRU pool** keyed by spec hash (`compute_spec_hash` over MCP server configs + stdio
  cwd, `mcp_manager.py:74`); lazy connect (`prewarm` / on-demand in `schemas_for`).
- Tool names **namespaced** `{server}__{bare}` (`_mcp_tool_schema`, `:98`); per-server allowlist
  (`spec.tools`) honoured against the bare name.
- `_resolve_tool_route` (`:387`) strips the server prefix and validates the bare tool exists on
  that server before dispatch.
- Inline custom-MCP `elicitation/create` → web card via `_build_elicitation_callback` (`:182`):
  POSTs `mcp_elicitation` to the server and parks on `pending_approvals`. Falls back to *decline*
  when no server client is available.

### 7.4 Native-harness MCP (in-turn relay vs out-of-turn serve-mcp)
- **`sys_*` (Omnigent) tools — in-turn relay:** the native vendor CLI calls `mcp__omnigent__*`
  tools, relayed by `ProxyMcpManager` → server `/mcp` (policy-checked centrally). Therefore the
  native **PreToolUse policy hook explicitly SKIPS `mcp__omnigent__*`** to avoid double-evaluation
  (`native_policy_hook.py:209,244`) — they're already gated by the relay path.
- **Connector/custom MCP (`mcp__github__*`) — still hook-gated:** these go through the native
  PreToolUse hook (`hook_payload_to_evaluation_request`, `:198`) → server `/policies/evaluate`.
- **Out-of-turn workspace tools — `serve-mcp`:** the native harness launches a separate
  `serve-mcp` MCP server the vendor discovers via its own settings.json; only the `sys_os_*`
  surface is registered there (workspace cwd, no sandbox) — per CUJ-ANALYSIS §2.C
  (`claude_native_bridge.py`, owned by the harness/native SME).

---

## 8. Per-harness differences (runner's view)

The runner is largely **harness-agnostic** at the tunnel/dispatch layer — `client_for_conversation`
only cares about the canonical harness string vs the runner's advertised `hello.harnesses`. The
differences surface in `app.py`:

- **claude-sdk / codex (SDK):** runner spawns an in-process executor subprocess; tool calls flow
  `harness → runner → ProxyMcpManager → server /mcp → /mcp/execute → runner`. Reasoning/transcript
  100% Omnigent. The trace corpus (`conv_63542a5f…`) is exactly this path.
- **claude-native / codex-native:** runner spawns the vendor CLI in a tmux pane; `sys_*` via
  in-turn relay (hook skips them), connector MCP via the PreToolUse hook → `/policies/evaluate`;
  `external_*` mirror events forwarded over the runner→server client. `_is_native_harness(conv)`
  gates several branches (e.g. `note_session_turn_started`, `app.py:14682`).
- **Polly / custom agents:** run on a chosen harness (typically claude-sdk) → identical runner
  path to that harness; the runner sees only the resolved spec + harness string.
- **codex / codex-native:** no live trace (AI-gateway creds expired); covered structurally — same
  dispatch/affinity/MCP-routing machinery (harness-agnostic), differing only in the spawned executor.

---

## 9. Failure branches & gaps

| Branch | Where | Behavior |
|---|---|---|
| Conversation not bound to a runner | `routing.py:112` | `OmnigentError(CONFLICT)` — "resume the session to bind a registered runner" |
| Bound runner offline | `routing.py:231` (+ `:139`, `:175`) | `RUNNER_UNAVAILABLE` |
| Runner lacks the harness | `routing.py:236` | `RUNNER_CAPABILITY_MISMATCH` |
| **Hard affinity, no failover** | `routing.py:89` (read-only; never picks/persists) | A bound runner going offline strands the session (CUJ-ANALYSIS §6) |
| Runner offline at request time | `transport.py:125` | `httpx.ConnectError` (identical handling to TCP connect refused) |
| Tunnel closes mid-request | `registry.deregister` `:299` → `_abort_session_inflight` | in-flight `head_future`/body aborted with `ConnectionError` (no hang) |
| Newer tunnel for same runner_id | `registry.register` `:280` "newest wins" | old session's in-flight aborted, old writer retired (close 4000) |
| Handshake: unauth non-loopback | `runner_tunnel.py:366` | close `4004` (fail-closed; no owner-less register) |
| Frame proto skew | `runner_tunnel.py:385` | close `4002` |
| WS auth redirect (Apps OAuth) | `serve.py:309` | fatal `RuntimeError` (retry can't help; tells user to `omnigent setup`) |
| Tunnel Bearer expiry, socket stays open | `serve.py:540` (once-at-open) | **no per-message refresh**; healed on next reconnect/recycle (§6.2) |
| Runner subprocess crash before bind | `process_manager._await_ready` | kills subprocess, raises `RuntimeError` |
| ASGI app crashes before head | `serve.py:207` | synthesizes 500 + `response.end` so server awaiter doesn't hang |
| MCP manager not configured | `app.py:17879,17957` | 503 `Runner MCP manager not configured` |
| No spec for session | `app.py:17899,17977` | `-32000` "No spec available" |
| Native sub-agent completions | (server-side gate `app.py:12496`, native excluded) | **🔴 silently never reach orchestrator** (CUJ-ANALYSIS §6 #848) — gate is server-side but the symptom hits native runs |

### Dedup (runner side)
The runner DOES dedup, but at three specific seams (not a global response-id cache):
1. **Inbound message ordering** — a **per-conversation FIFO ingest gate**
   (`_ingest_next_seq` / `_ingest_now_serving` / `_ingest_cond`, `app.py:14692`,
   RUNNER_MESSAGE_INGEST.md Part A): each arrival takes a slot synchronously (before any await),
   then waits its turn so content-resolution latency can't reorder messages. The
   **single-active-turn invariant I2** (`_active_turns`, `app.py:14711`) buffers/steers mid-turn
   messages.
2. **History-load dedup** — when rebuilding the turn's input history the runner drops the
   just-arrived item by `persisted_item_id` (`_load_history_as_input(drop_item_id=…)`,
   `app.py:14842`) so the new message isn't counted twice (once from the store, once as the new
   input).
3. **Mid-turn injection exactly-once** — `injection.consumed` markers (`app.py:14251`,
   RUNNER_MESSAGE_INGEST.md Part B): once the harness consumes a mid-turn injection into the live
   turn, the buffered copy is dropped so it doesn't also drive a continuation turn. The native
   transcript reader keeps an **in-memory seen-set** (the durable cursor was retired,
   `app.py:4300`) and the native log accumulator uses **generation-id dedup** to make re-reading
   idempotent (`app.py:2112`, `8236` "watcher already dedupes to edges").

What the runner does **not** do: dedup *forwarded outbound* events by item-id. Durable dedup is
the **client's** job (by `ctx.itemId`) and durable persistence is the **server's**
(`conversation_store.append`); code comments flag "there is no server-side dedup"
(`app.py:5844`) and a post-recovery double-persist risk (`app.py:321`). So the FIFO-desync bug
class (CUJ-ANALYSIS §6) lives at the ingest gate + the cross-layer itemId contract.

---

## 10. Open questions
- Does any deployed path still write the **one-shot `OMNIGENT_POLICY_AUTH`** snapshot for an
  in-scope native harness (claude-native)? Confirmed it survives for the **opencode** plugin path
  (`app.py:1141`, degrades fail-**open**), while cursor/hermes use the refreshable
  `policy_hook_reauth` path. Needs the harness/native SME to confirm claude-native's exact wiring
  (PR #1439 claim). → cross-ref policy/auth SME.
- Exact semantics of `replace_runner_id` (`sqlalchemy_store.py:1951`) on **resume/fork** — who
  calls it and whether it ever enables a soft "re-bind to a fresh runner" that would relax the
  no-failover stance. (Resume re-binds via `PATCH /v1/sessions/{id}` per `routing.py` docstrings.)
- Whether `on_reconnect=catch_up_scan` (`_entry.py:958`) can double-deliver after a recycle
  (the "post-recovery persisted twice" comment, `app.py:321`).
