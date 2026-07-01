# CUJ Answers — Runner dispatch, MCP routing, request lifecycle, channel auth, dedup

**Domain:** the RUNNER (`omnigent/runner/`) + runner↔server reverse WS tunnel + harness/executor
UDS boundary. In-scope harnesses: claude (sdk + native), codex (sdk + native), Polly.

> Every mechanism cited `file:line` (code = ground truth) and, where possible, backed by the live
> trace `conv_63542a5f92e24956812e19b104eac0e9` (claude-sdk tool-use; trace `cfb59197f6f9…`).
> codex/codex-native have **no live trace** (AI-gateway creds expired) — covered structurally;
> the runner dispatch/affinity/MCP machinery is **harness-agnostic** so it applies identically.

---

## Q1. Runner dispatch & affinity (hard binding, no failover; CONFLICT / RUNNER_UNAVAILABLE / capability mismatch)

**Mechanism:** `RunnerRouter.client_for_conversation(conversation_id, harness)`
(`routing.py:89`). Dispatch is **read-only for affinity** — it never picks or persists a runner
(docstring `routing.py:93-95`). The decision tree:

```
conv = conversation_store.get_conversation(id)
├─ conv is None                       → OmnigentError(NOT_FOUND)
├─ conv.runner_id is None             → OmnigentError(CONFLICT)   "not bound to a runner; resume to bind"   (routing.py:112)
└─ conv.runner_id set → _routed_pinned_runner(runner_id, harness)  (routing.py:220)
     ├─ registry.get(runner_id) is None        → RUNNER_UNAVAILABLE  "runner offline; resume to bind"      (routing.py:231)
     ├─ not _runner_supports_harness(session, harness) → RUNNER_CAPABILITY_MISMATCH                          (routing.py:236)
     └─ RoutedRunner(runner_id, client=_client_for_runner(runner_id))   # cached WSTunnelTransport client
```

- **Hard binding:** the conversation's `runner_id` is set **once** via an atomic CAS,
  `set_runner_id` = `UPDATE … WHERE id=:id AND runner_id IS NULL` (`sqlalchemy_store.py:1918`).
  Concurrent first-dispatches race; exactly one wins, the loser's UPDATE affects 0 rows → re-read
  to discover the winner. Binding happens via `PATCH /v1/sessions/{id}` (server side), **not** in
  dispatch.
- **No failover / no rebalance:** there is no code path that re-routes a bound conversation to a
  different online runner. A bound runner going offline → every dispatch raises
  `RUNNER_UNAVAILABLE` until that exact runner reconnects (its `runner_id` is stable across
  restarts — `get_stable_runner_id`, persisted). Confirmed gap in CUJ-ANALYSIS §6
  ("Hard runner affinity, no failover — a bound runner going offline strands the session").
- **Capability check:** `_runner_supports_harness` (`routing.py:269`) tests the canonicalized
  harness string against the runner's advertised `hello.harnesses`. The hello set is
  `[claude-native, claude-sdk, codex, openai-agents, open-responses, pi]` (`serve.py:582`) — note
  **codex-native is NOT in the default hello list**, so a codex-native dispatch to a stock runner
  would raise `RUNNER_CAPABILITY_MISMATCH` unless that runner advertises it. (Native harnesses are
  largely CLI-driven from the host, not server-dispatched over the tunnel — see Q2/resume.)
- **`replace_runner_id`** (`sqlalchemy_store.py:1951`) exists for **explicit re-bind** (PATCH
  callers, or internal sub-agent rebind to the parent's current runner) — this is the only way the
  binding changes, and it's caller-driven, not automatic failover.

**Sibling routing helpers** (same affinity): `client_for_session_resources` (`:118`, terminals/
files), `client_for_existing_conversation` (`:154`, interrupt/terminal-list — returns `None` when
unpinned so callers fall back to in-process test behavior).

---

## Q2. Resume dispatch — which harness gets relaunched (`resume_dispatch.py`)

`omnigent resume` is **CLI glue, not a runner-internal mechanism** (`resume_dispatch.py:39`
`run_resume`). It answers "take me back to where I was" for **terminal-native** sessions by
relaunching the right vendor wrapper:

1. **Resolve the target:** direct id (`omnigent resume conv_…`) or the cross-agent picker
   (`omnigent resume` with `--server`, `_pick_conversation_for_resume`, `:89`).
2. **Read the wrapper label:** `_dispatch_by_runtime` (`:147`) GETs the conversation and reads
   `labels.omnigent.wrapper` (remote: `_read_wrapper_label_remote` `:339`; local sqlite:
   `_read_wrapper_label_local` `:312`).
3. **Dispatch by runtime:** `_dispatch_wrapper` (`:201`) maps the wrapper label →
   `native_coding_agent_for_wrapper_label` → the matching `run_<harness>_native(...)`:
   - `claude` → `run_claude_native` (`:219`), `codex` → `run_codex_native` (`:228`),
     `pi` → `run_pi_native`, plus cursor/kiro/goose/antigravity/qwen/kimi/hermes.
   - **The relaunched harness is whichever native wrapper minted the session** (recorded in the
     wrapper label at creation). The native wrapper owns its own local-server + runner lifecycle.
4. **No wrapper label (i.e. an SDK session):** `_dispatch_wrapper` returns `False`, and
   `run_resume` raises a `ClickException` pointing the user at `omnigent run --resume <conv>
   <agent.yaml> [--server …]` (`:179`, `:193`). So **SDK harnesses (claude-sdk, codex, Polly) do
   not resume via `resume_dispatch.py`** — they resume through `omnigent run --resume`, which goes
   through the normal create-session → bind-runner → dispatch path.

**How much transcript loads into the runner on resume** (runner's side): when a turn is
dispatched after resume, the runner pulls the transcript itself — `GET /v1/sessions/{id}/items`
and reconstructs input via `_load_history_as_input` (`app.py:14842`, with the `persisted_item_id`
dedup drop). SDK harnesses load conversation history into the executor; native harnesses rebuild
from stored items / `FORK_CARRY_HISTORY`. (Detail owned by harness/native SME; runner's role is
the `GET …/items` fetch — visible in the trace as `omni-runner → omni-server GET …/items`.)

---

## Q3. MCP routing — custom (user) MCP vs Omnigent `sys_*`; who routes a tool call where

**The single most important runner fact: there are TWO MCP managers with the same interface,
chosen by mode** (`proxy_mcp_manager.py:15-23`):

| Mode | Manager | Policy enforced by | MCP connections held by |
|---|---|---|---|
| **Deployed** (runner + server) | `ProxyMcpManager` | **Server** (`/mcp` → `_evaluate_tool_call_policy`) | **Server** `ServerMcpPool`, delegates **execution** to runner |
| **No-server / test** | `RunnerMcpManager` | **Runner** (`RunnerToolPolicyGate`, `policy.py`) | **Runner** (direct stdio/HTTP, 8-entry LRU) |

### The deployed routing loop (verified in the trace)
1. Harness emits a tool call → runner `ProxyMcpManager.call_tool` POSTs `tools/call` to server
   `POST /v1/sessions/{id}/mcp` (`proxy_mcp_manager.py:220`). **Trace:** `omni-runner →
   omni-server [POST …/mcp]`.
2. Server runs **TOOL_CALL** policy (`omni-server policy.evaluate` span), then calls **back into
   the runner**: `POST /v1/sessions/{id}/mcp/execute` (`app.py:17829`). **Trace:** `omni-server →
   omni-runner [POST …/mcp/execute]`. The split is deliberate: *server owns policy + routing;
   runner owns execution* (tools must run on the right machine, cwd, env — `app.py:17834-17837`).
3. **The routing key is the `__` separator** (`app.py:17954`):
   - **`"__" in tool_name`** (e.g. `github__search`, `glean__search`) → **custom/user MCP** →
     `RunnerMcpManager.call_tool` → live stdio/HTTP MCP subprocess (`app.py:18011`). The
     `{server}__{tool}` name is validated against the server prefix then stripped
     (`mcp_manager._resolve_tool_route`, `:387`).
   - **no `__`** (e.g. `sys_os_read`, `sys_terminal_launch`) → **runner-local Omnigent builtin**
     → `execute_tool` (`tool_dispatch.py`) with the session's terminal registry / inbox / sandbox
     workspace (`app.py:18070`). Comment: "All MCP tools are namespaced … so any name without `__`
     is definitively a runner-local tool" (`app.py:18046`).
4. Server runs **TOOL_RESULT** policy on the output (second `policy.evaluate` span), returns the
   (possibly transformed/denied) result to the runner, which feeds the harness.

So **for SDK harnesses (claude-sdk/codex/Polly): both `sys_*` AND custom MCP go through the same
server `/mcp` → `/mcp/execute` round-trip**; the only divergence is the `__`-based fork inside
`/mcp/execute` (custom → RunnerMcpManager; `sys_*` → execute_tool).

### Native harness MCP — in-turn relay vs out-of-turn serve-mcp
- **`sys_*` (Omnigent) — in-turn relay:** the vendor CLI calls `mcp__omnigent__*` tools relayed by
  `ProxyMcpManager` → server `/mcp` (centrally policy-checked). **Therefore the native PreToolUse
  policy hook explicitly SKIPS `mcp__omnigent__*`** to avoid double-evaluation
  (`native_policy_hook.py:209-211, 244`): "already policy-checked by the relay path."
- **Connector/custom MCP (`mcp__github__*`) — hook-gated:** these still go through the native
  PreToolUse hook → `hook_payload_to_evaluation_request` (`:198`) → server `/policies/evaluate`.
- **Out-of-turn workspace tools — `serve-mcp`:** the native harness launches a separate `serve-mcp`
  MCP server the vendor discovers via its own settings.json; only `sys_os_*` is registered there
  (workspace cwd, no sandbox). The runner seeds its relay token at boot
  (`write_relay_bridge_config`, `app.py:1054`). (Bridge details owned by harness/native SME.)

### Custom-MCP elicitation (approval from a user MCP server)
- `RunnerMcpManager._build_elicitation_callback` (`mcp_manager.py:182`): on inline
  `elicitation/create`, POST `mcp_elicitation` to the server and park on
  `pending_approvals.wait_for_user_approval`; decline if no server client.
- Through the proxy, a custom MCP's `InputRequiredResult` surfaces as `McpElicitationRequired` back
  through `/mcp/execute` (`app.py:18017`) → server owns the elicitation; retry uses
  `call_tool_with_elicitation` with `inputResponses`/`requestState` (MRTR, `app.py:18004`).

---

## Q4. Request lifecycle from the runner's side (forwarded user event → executor → tool dispatch → persist/forward back)

**Entry point:** `POST /v1/sessions/{conversation_id}/events` (`app.py:14598`
`post_session_events`) — the runner receives the user event the server forwarded over the tunnel.
Body is the harness discriminated union (`message` / `interrupt` / `tool_result` / `approval`).

Step by step (`message` path, `stream=true` — what the trace shows):
1. **FIFO ingest gate** (`app.py:14692`, RUNNER_MESSAGE_INGEST Part A): take an arrival slot
   synchronously (read-increment `_ingest_next_seq` before any await), wait on `_ingest_cond`
   until `_ingest_now_serving` reaches this seq → guarantees per-conversation arrival order.
2. **Content resolution:** `_resolve_forwarded_message_content` (`app.py:14704`) hydrates content
   blocks (e.g. fetches referenced items via `server_client`).
3. **Single-active-turn gate (invariant I2):** if `conversation_id in _active_turns`, the message
   is buffered or steered (parked-on-approval turns are NOT steered, `app.py:14711-14717`).
4. **History rebuild:** `_load_history_as_input(drop_item_id=persisted_item_id)` (`app.py:14842`)
   — loads transcript, drops the just-arrived item to avoid double-count. **Trace:** `omni-runner
   → omni-server [GET …/items]` and `[GET …/agent/contents]` (spec bundle via `spec_resolver`).
5. **Executor dispatch:** the runner POSTs the turn to the per-conversation harness/executor
   subprocess over **UDS** (`process_manager`, `/tmp/omnigent` socket). **Trace:** `omni-runner →
   omni-harness [POST …/events]`. MCP schemas are injected into the event body
   (`_inject_mcp_schemas`, `app.py:6850`).
6. **Stream relay (`proxy_stream`, `app.py:14115`):** the SSE body from the harness is relayed
   chunk-by-chunk back to the server (`StreamingResponse`, `media_type="text/event-stream"`,
   `app.py:14592`). The relay also:
   - intercepts `action_required` events for **local tool dispatch** (the `/mcp` round-trip, Q3),
   - intercepts `policy_evaluation.requested` → `_evaluate_policy_via_omnigent` (Q5),
   - accumulates text / tool-calls / tool-results into `_session_histories`,
   - `_publish_event` puts events on the per-session queue for `GET …/stream`.
7. **Turn finalize:** `_on_proxy_stream_end` (`app.py:12519`) pops `_active_turns`, clears
   in-flight, publishes `session.status` = idle / failed / waiting, schedules a post-turn buffer
   check (drains any buffered messages queued during the turn).

**Persist-vs-forward:** the runner **forwards** (streams) turn output; **durable persistence is
the server's** (`conversation_store.append` on the server side, after receiving the SSE). The
runner streams `response.output_text.delta` and final items back; the server persists. The
`stream=false` variant returns 202 and events flow via `GET …/stream` + a background
`_run_turn_bg` task.

---

## Q5. Runner↔server channel & auth — handshake, per-message vs once-at-open Bearer, header-verbatim forwarding

### The handshake (runner side `serve.py:494`, server side `runner_tunnel.py:277`)
The runner is the WS **client**. `_serve_tunnel_once` opens `ws(s)://<server>/v1/runners/
{runner_id}/tunnel` (`serve.py:543`) with handshake headers:
- `Origin: omnigent://internal` (first-party sentinel → clears server CSWSH/origin guard),
- `Authorization: Bearer <token>` + `X-Databricks-Org-Id` (`databricks_request_headers`, `:540`),
- `X-Omnigent-Runner-Tunnel-Token: <binding_token>` (`RUNNER_TUNNEL_TOKEN_HEADER`, `:542`).

Server validates **before accept**: token gate / expected-runner-id (close `4004` on mismatch);
owner resolution **fail-closed** for unauthenticated non-loopback (would otherwise register
`owner=None` and bypass owner-scoped guards, `runner_tunnel.py:331-371`); then `accept()`, receive
hello, **strict-major `frame_protocol_version`** check (close `4002`), `registry.register`.
Register is **"newest wins"** (`registry.py:280`): a new tunnel for the same `runner_id` discards
the old session and aborts its in-flight requests with `ConnectionError`.

### Per-message vs once-at-open Bearer (the KNOWN GAP — and its mitigation)
- **The Bearer is injected ONCE, in the WS upgrade headers** (`serve.py:540`). The long-lived
  socket then carries every server→runner `request` frame with **no per-message re-auth** — there
  is no hook to re-set a header on an open WS. This is the runner↔server "no per-message refresh"
  item in CUJ-ANALYSIS §2.G / §6.
- **Mitigations that make it mostly self-healing:**
  - `serve_tunnel` re-mints the token on **every reconnect** (`_refresh_auth_token`, `serve.py:284`,
    via `auth_token_factory`).
  - On a handshake **HTTP 401**, refresh once and retry (`_handle_refreshable_auth_failure`,
    `serve.py:326`); on a fatal 403, raise (`serve.py:332`).
  - Routine ingress recycles — close `1001`/`1012`, HTTP `502` — reset backoff to the minimum and
    reconnect promptly (`serve.py:343-353`, `_TUNNEL_RECYCLE_*`), which **also** re-mints.
  - WS auto-redirect to an http(s) OAuth login (Apps unauthenticated) is **fatal** with a clear
    "run `omnigent setup`" hint (`serve.py:309`, `_websocket_auth_redirect_url`).
  - So a token expiry only bites if the socket stays open *past* expiry **with no recycle and no
    new request that triggers a server-side re-auth**; the next reconnect heals it.
- **The runner→server direction has NO such gap:** `_RunnerDatabricksAuth.auth_flow`
  (`_entry.py:192`) mints a **fresh token per request** and retries once on 401 or Apps
  `302→/oidc/` (`_is_login_redirect_or_unauthorized`, `:241`). One SDK `Config` is cached for the
  factory's lifetime so the token comes from the SDK in-memory cache (no per-request CLI shellout,
  `_entry.py:312-360`). `follow_redirects=False` deliberately so the auth flow *sees* the Apps
  login 302 and can re-mint (`create_app`, `app.py`/`_entry.py:732-739`).

### Header-verbatim forwarding (how trace context crosses)
- `WSTunnelTransport.handle_async_request` (server side) forwards request headers **verbatim**:
  `headers=[[k, v] for k, v in request.headers.items()]` (`transport.py:149`). On the runner side
  `dispatch_via_asgi` rebuilds the ASGI scope headers from the frame (`serve.py:126`). So whatever
  the server's httpx client injected (incl. `traceparent`) reaches the runner's FastAPI extractor
  unchanged.
- **The custom-transport propagation fix:** the server→runner client is built on the custom
  `WSTunnelTransport`, invisible to the process-wide `HTTPXClientInstrumentor`. So the cached
  per-runner client is instrumented **directly**: `telemetry.instrument_httpx_client(client)`
  (`routing.py:264`, `telemetry.py:414`). This injects `traceparent` on the forward; combined with
  `create_runner_app` calling `telemetry.instrument_fastapi_app(app)` (`app.py:7775`) and the
  verbatim forwarding, the server→runner hop stays in **one connected trace**. **Verified in the
  trace:** runner spans nest under server spans (§Trace evidence). This is exactly OBSERVABILITY.md
  §12 "custom-transport client gap (resolved)".

---

## Q6. Dedup — runner side

The runner dedups at **three specific seams** (no global response-id cache):
1. **Inbound order, not duplicates:** the per-conversation FIFO ingest gate
   (`app.py:14692`) preserves arrival order; the single-active-turn gate (`_active_turns`,
   `app.py:14711`) buffers/steers mid-turn arrivals so they don't spawn overlapping turns.
2. **History-load dedup:** `_load_history_as_input(drop_item_id=persisted_item_id)`
   (`app.py:14842`) drops the just-arrived item so it isn't counted twice (store + new input).
3. **Mid-turn injection exactly-once:** `injection.consumed` markers (`app.py:14251`,
   RUNNER_MESSAGE_INGEST Part B) — once the harness consumes an injection into the live turn, the
   buffered copy is dropped so it doesn't also drive a continuation turn. The native transcript
   reader keeps an **in-memory seen-set** (durable cursor retired, `app.py:4300`); the native log
   accumulator uses **generation-id dedup** for idempotent re-reads (`app.py:2112`; "watcher
   already dedupes to edges", `app.py:8236`).

**What the runner does NOT dedup:** *forwarded outbound* events by item-id. Durable dedup is the
**client's** job (by `ctx.itemId`) and durable persistence is the **server's**
(`conversation_store.append`). Code flags this: "there is no server-side dedup" (`app.py:5844`)
and a post-recovery double-persist risk after a tunnel recovery scan (`app.py:321`,
`on_reconnect=catch_up_scan`). **The FIFO-desync bug class** (CUJ-ANALYSIS §6, native-firstmsg-
fifo-desync) lives at the ingest gate + the cross-layer itemId contract — not in a runner cache.

---

## Per-harness notes (runner's view)

The runner is **harness-agnostic at the tunnel/dispatch/MCP-routing layer**. Differences:

| Harness | Dispatch over tunnel | `sys_*` routing | Custom MCP routing | Live trace? |
|---|---|---|---|---|
| claude-sdk | yes (capability ✅) | server `/mcp` → `/mcp/execute` (no `__` → execute_tool) | server `/mcp` → `/mcp/execute` (`__` → RunnerMcpManager) | ✅ `conv_63542a5f…` |
| codex (sdk) | yes (capability ✅) | same as claude-sdk (harness-agnostic) | same | ❌ creds expired (structural) |
| claude-native | CLI/host-driven; relay in-turn | in-turn relay; PreToolUse hook **skips** `mcp__omnigent__*` | PreToolUse hook → `/policies/evaluate`; serve-mcp out-of-turn for `sys_os_*` | partial (native corpus `conv_d0ddd6b3…`) |
| codex-native | CLI/host-driven | in-turn relay (analogous) | hook → `/policies/evaluate` | ❌ creds expired (structural) |
| Polly / custom agents | runs on chosen harness (usually claude-sdk) → **inherits that row** | inherits | inherits | via claude-sdk |

---

## Failure-branch summary (runner dispatch + MCP)

| Code | Condition | Site |
|---|---|---|
| `CONFLICT` | conversation not bound to a runner | `routing.py:112` |
| `RUNNER_UNAVAILABLE` | bound runner offline | `routing.py:231` / `:139` / `:175` |
| `RUNNER_CAPABILITY_MISMATCH` | runner's hello lacks the harness | `routing.py:236` |
| `httpx.ConnectError` | runner offline at request time (tunnel) | `transport.py:125` |
| in-flight `ConnectionError` | tunnel closed mid-request / replaced | `registry.deregister` / `register` "newest wins" |
| close `4004`/`4001`/`4002` | bad token / no hello / proto skew (handshake) | `runner_tunnel.py:304/380/385` |
| fatal `RuntimeError` | WS bounced to OAuth login (Apps unauth) | `serve.py:309` |
| 503 "MCP manager not configured" | `mcp_manager is None` on `/mcp/execute` | `app.py:17879/17957` |
| `-32000` "No spec available" | spec unresolved for session | `app.py:17899/17977` |
| `-32000 input_required` / `McpElicitationRequired` | custom MCP elicits | `app.py:18017` (MRTR retry) |
| **no failover (gap)** | bound runner offline strands session | `routing.py:89` (CUJ-ANALYSIS §6) |
| **Bearer once-at-open (gap)** | tunnel token expiry w/o reconnect | `serve.py:540` (CUJ-ANALYSIS §2.G) |

---

## Open questions / things to cross-check with other SMEs
- **Policy/auth SME:** confirm whether claude-native still uses the one-shot
  `OMNIGENT_POLICY_AUTH` snapshot (fail-open, `app.py:1141` is the *opencode* path) or the
  refreshable `policy_hook_reauth` path (`native_policy_hook.py:133`) — i.e. is the
  fail-closed-after-1h bug (PR #1439) actually closed for the in-scope native harness? The
  refreshable mechanism exists and re-mints on 401/Apps-302; the legacy snapshot survives in some
  paths and degrades fail-**open** there.
- **Server SME:** the server side of `/mcp` (ServerMcpPool, `_evaluate_tool_call_policy`) and the
  PATCH-driven `set_runner_id` binding flow — the runner only consumes the binding.
- **Harness/native SME:** transcript-into-runner volume on resume/fork, the `serve-mcp` bridge,
  and the native sub-agent-completion gate (`app.py:12496`, native excluded — CUJ-ANALYSIS §6 #848).
