# Holistic Distributed Tracing for Omnigent

**Status:** Proposed
**Scope:** End-to-end visibility into all data flowing between Omnigent's distributed
components, using the official OpenTelemetry clients with real W3C trace-context
propagation across every transport boundary.

---

## 1. Motivation

Omnigent is a distributed, multi-process system (host daemon, runners/harnesses,
server, clients, database). It is heavily vibe-coded and lacks a clear mental map,
which makes stability and reliability work hard. Static analysis alone has proven
unreliable; we want to incorporate signal from **real usage** by tracing every RPC,
message, and cross-process call.

Today there is a partial telemetry layer (`omnigent/runtime/telemetry.py`) built on
MLflow Tracing + OpenTelemetry, but:

- Trace context is **never propagated over the wire**. Instead each layer on the
  agent-turn path independently derives the same W3C trace ID from a shared
  `response_id` (`telemetry.py:307`, `:340`). Elegant, but it only covers boundaries
  that carry a `response_id`.
- Everything **without** a `response_id` is dark: host-daemon control frames,
  client REST/SSE control traffic, session-list updates, the native policy HTTP hook,
  and all database queries.
- `HTTPXClientInstrumentor` is a declared dependency but **never wired**.
- `FastAPIInstrumentor` is gated off by default.
- `get_traceparent_env()` (the one real OTel subprocess-propagation helper) is
  **dead code** — zero call sites.
- No SQLAlchemy instrumentation.

This design replaces the "derive-the-same-id-everywhere" convention with **standard
OpenTelemetry context propagation**: inject a W3C `traceparent` at every send site,
extract it at every receive site. The deterministic `response_id → trace_id`
derivation is kept only as the **root trace-ID seed** so operators can still look up a
trace by response ID — but cross-boundary continuity comes from real propagation.

---

## 2. Goals / Non-goals

### Goals

- One `trace_id` flows edge-to-edge across **every** inter-component boundary, so a
  single user action renders as one connected trace spanning every component it
  touched.
- Use **only the official `opentelemetry-*` clients** and the W3C Trace Context
  standard. No bespoke propagation scheme.
- A **locally runnable** tracing backend for development and tests, swappable for a
  production backend via standard OTLP env vars.
- Every boundary span records direction, message/operation type, size, latency,
  status, and (behind a flag) payload content.

### Non-goals

- Quality/eval scoring of agent behavior (OTel captures structure, not correctness).
- Replacing the durable conversation/event store. A durable append-only event log for
  transcript reconstruction is complementary and tracked separately (see §9).
- Log aggregation redesign. We bridge Python `logging` to OTel so logs carry
  `trace_id`/`span_id` (already implemented in `telemetry.py`), but the logging
  pipeline itself is out of scope.

---

## 3. Chosen backend: Jaeger all-in-one (local), OTLP everywhere

**Primary local/test backend: Jaeger `all-in-one`.**

Rationale:

- **Single container, zero config.** One image runs the collector, storage
  (in-memory), and a query UI.
- **Native OTLP ingest** on the standard ports — gRPC `4317` and HTTP `4318` — so the
  application is configured purely through standard `OTEL_EXPORTER_OTLP_*` env vars and
  nothing is Jaeger-specific in our code.
- **Built-in trace UI** at `:16686` with service-dependency and span-waterfall views —
  ideal for verifying that a trace actually spans daemon → server → runner → harness.
- **Disposable.** In-memory storage means a fresh, clean state on every restart, which
  is exactly what local iteration and integration tests want.

```bash
# Local backend for dev + tests
docker run --rm --name jaeger \
  -p 16686:16686 \   # Jaeger UI
  -p 4317:4317 \     # OTLP gRPC
  -p 4318:4318 \     # OTLP HTTP
  jaegertracing/all-in-one:latest
# Trace UI: http://localhost:16686
```

**Production / larger scale (later):** the same OTLP export points at **Grafana Tempo**
(traces) alongside Loki (logs) and Prometheus (metrics) for a unified stack, or any
OTLP-compatible vendor. Because we standardize on OTLP, **no application code changes**
when swapping backends — only `OTEL_EXPORTER_OTLP_ENDPOINT`.

> Backends considered and why not, for the local case: Grafana Tempo + Grafana
> (more moving parts than needed for a laptop), Arize Phoenix / Langfuse / MLflow
> (LLM-eval-oriented, not general distributed-systems tracing), SaaS (Datadog/Honeycomb;
> not local). Jaeger all-in-one is the simplest thing that gives a real waterfall UI.

---

## 4. Official OpenTelemetry clients

All instrumentation uses upstream OpenTelemetry packages — no custom propagation code.

| Package | Purpose | Status in repo |
|---|---|---|
| `opentelemetry-sdk` | TracerProvider, span processors, resources | transitive (present) |
| `opentelemetry-exporter-otlp-proto-grpc` | OTLP/gRPC span+metric+log export | declared |
| `opentelemetry-exporter-otlp-proto-http` | OTLP/HTTP export (alt protocol) | declared |
| `opentelemetry-instrumentation-fastapi` | Server-side HTTP span + `traceparent` extract | declared, gated off |
| `opentelemetry-instrumentation-httpx` | Client-side HTTP span + `traceparent` inject | **declared, not wired** |
| `opentelemetry-instrumentation-sqlalchemy` | DB query spans | **missing — add** |
| `opentelemetry-distro` | `opentelemetry-instrument` zero-code agent (dev probe only) | declared |

Propagation uses the official APIs directly for the non-HTTP boundaries:

- `opentelemetry.propagate` (`inject` / `extract`) with the default global
  `TraceContextTextMapPropagator` (W3C `traceparent`/`tracestate`).
- `opentelemetry.trace` for manual spans at choke points that auto-instrumentation
  cannot see (websocket message frames).
- `opentelemetry.context` `attach` / `detach` to make an extracted remote context the
  active context on the receiving side.

> The global propagator is W3C Trace Context by default; this design relies on that
> default and does not register a custom propagator.

---

## 5. Propagation model

### 5.1 Principle

At **every** boundary where data crosses a process or network edge:

1. **Inject** the current trace context into the outbound carrier on the send side.
2. **Extract** it on the receive side and `attach` it so spans created there nest under
   the caller's trace.

The carrier differs by transport, but the API is uniform (`inject(carrier)` /
`extract(carrier)`).

### 5.2 Root trace-ID seed (kept)

For request roots that own a `response_id`, the **root span's** trace ID is still seeded
deterministically from the response ID (`trace_id_from_response_id`,
`telemetry.py:271`) so operators can jump from a response ID to its trace with no lookup
table. This is purely a root-ID convention; **all downstream continuity comes from W3C
propagation**, not re-derivation. Boundaries with no `response_id` (host control frames,
session-list updates) simply get a normal generated trace ID and propagate it.

### 5.3 Five transport techniques

| Transport | Technique | Carrier |
|---|---|---|
| HTTP (REST, SSE handshake, policy hook) | Auto: FastAPI extract + HTTPX inject | HTTP headers |
| WS reverse-tunnel (runner ↔ server) | Auto — headers forwarded verbatim (`transport.py:149`); HTTPX/FastAPI do the work | HTTP headers tunneled in `request` frame |
| WS control frames (host tunnel, session-updates) | **Manual** `inject`/`extract` into the JSON envelope | new `traceparent` field on the frame |
| Subprocess (harness/executor over UDS) | Auto via tunneled HTTP headers; optional revive of `get_traceparent_env()` | HTTP headers / env |
| Database (SQLAlchemy) | Auto: `SQLAlchemyInstrumentor` (sink, no propagation needed) | n/a |

---

## 6. Per-boundary instrumentation spec

Components, as named in the request: **Host Daemon, Runners (Harnesses), Web UI, TUI,
Server, Policy Server, Server database.** Choke points below are from the codebase map.

### 6.1 Web UI ↔ Server

- **REST commands** (`POST /v1/sessions/{id}/events`, session CRUD, `/v1/me`,
  `/v1/info`): server-side `FastAPIInstrumentor` extracts incoming `traceparent`;
  browser `fetch` sets it via the OTel web SDK (or, minimally, the server starts the
  root and the response carries the trace ID back for client-side correlation).
- **SSE chat stream** (`GET /v1/sessions/{id}/stream`): the HTTP handshake is traced by
  FastAPI. Each streamed event is annotated with the active `trace_id` in
  `_format_sse` (`sessions.py:1754`) so the client can correlate UI blocks to the trace.
- **Browser-origin propagation (implemented):** `web/src/lib/telemetry.ts`
  (`initBrowserTelemetry`, called first in `main.tsx`) initializes the OpenTelemetry
  **web SDK** and registers `@opentelemetry/instrumentation-fetch` and
  `-xml-http-request` so every `fetch`/SSE call carries a browser-rooted `traceparent`.
  A trace therefore begins at the user's click in the browser, not at the server. It is
  **opt-in by configuration** — active only when `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is
  set (mirroring the server's "on when a backend is configured" rule); otherwise it is a
  no-op with zero overhead. The browser exports over OTLP/HTTP to `${endpoint}/v1/traces`.
  Service name `omni-web` (override via `VITE_OTEL_SERVICE_NAME`). **CORS:** Omnigent is a
  same-origin deployment — the server serves the SPA and the API from one origin (vite
  proxies in dev), and there is **no `CORSMiddleware`** — so `traceparent` propagates to
  same-origin API calls with no server change. `propagateTraceHeaderCorsUrls` is scoped to
  the app's own origin so the header attaches explicitly and is never leaked to unrelated
  third-party requests; a future cross-origin/embedded deployment that adds CORS must add
  `traceparent`/`tracestate` to its allowed request headers.
- **Session-updates WebSocket** (`/v1/sessions/updates`): **manual**. Inject in
  `_send(frame)` (`sessions.py:14076`); extract in the `_reader()` loop
  (`sessions.py:14117`). Add a `traceparent` field to the envelope
  (`{"type": "...", "traceparent": "..."}`).
- **Terminal-attach WebSocket**: extract at handler `terminal_attach.py:130`, inject in
  `_shuttle_ws_frames` (`terminal_attach.py:324`).

### 6.2 TUI ↔ Server

- REST + SSE via `OmnigentClient`'s single `httpx.AsyncClient` (`_client.py:89`). Wiring
  `HTTPXClientInstrumentor` injects `traceparent` on every outbound call automatically —
  one line, covers the entire TUI boundary.

### 6.3 Runners (Harnesses) ↔ Server

- **WS reverse-tunnel** (`runner_tunnel.py:146` server; `ws_tunnel/serve.py:230`
  client). The tunnel **forwards HTTP headers verbatim** (`transport.py:149`), so once
  the server's outbound httpx call carries an injected `traceparent` and **both** the
  server app and the runner ASGI app are FastAPI-instrumented, the trace crosses the
  tunnel with **no envelope change**. Confirm `instrument_fastapi_app` runs on the
  runner app, not just the server (`app.py:1254`).
- **Harness/executor subprocess** (HTTP over Unix socket, `process_manager.py:966`):
  covered by the same tunneled-header propagation; the existing
  `trace_context_for_response` (`_executor_adapter.py:398`) remains the fallback. Reviving
  `get_traceparent_env()` (`telemetry.py:426`) is optional once HTTP propagation works.

### 6.4 Host Daemon ↔ Server

- **WS, JSON control frames** (`host/frames.py`), *not* HTTP — auto-instrumentation
  cannot see these. **Manual**: add a `traceparent` field to the frame envelope; inject
  in `_serve_frames` (`connect.py:1432`) on send, extract in `_receive_loop`
  (`host_tunnel.py:312`) on receive, and start a span per `HostFrameKind`
  (`host.launch_runner`, `host.stop_runner`, `host.runner_exited`, fs ops). The
  existing per-request `request_id` becomes a span attribute.
- **Host → Runner spawn**: one-way via env at spawn (`connect.py:391`). Add
  `TRACEPARENT` to the spawn-env allowlist (`connect.py:313`) **only if** a span is
  active at spawn time; otherwise the runner roots its own trace (launch is a daemon
  control action, not part of a user request).

### 6.5 Policy "Server"

- There is **no separate policy process.** Default enforcement is an in-process Python
  call (`policies/engine.py:42`) → wrap each evaluation in a `policy.evaluate` span with
  decision/reason attributes.
- **Native-harness HTTP hook** (`POST /v1/sessions/{id}/policies/evaluate`,
  `sessions.py:15438`; client `native_policy_hook.py`): covered free by FastAPI extract
  + HTTPX inject once both are wired.

### 6.6 Server ↔ Database

- **Sync SQLAlchemy** (sqlite / psycopg, wrapped in `asyncio.to_thread`); engines built
  in `db/utils.py` (`get_or_create_engine`, `:292`; `_create_engine`, `:198`).
- Add `opentelemetry-instrumentation-sqlalchemy` and call
  `SQLAlchemyInstrumentor().instrument(engine=engine)` for each engine at creation in
  `db/utils.py`. Every query becomes a child span under the active request trace.

---

## 7. SDK / provider setup

Build on the existing `omnigent/runtime/telemetry.py` `init()`; it already establishes a
**unified global `TracerProvider`** shared between MLflow and raw OTel
(`MLFLOW_USE_DEFAULT_TRACER_PROVIDER=false`) and flips OTLP export on when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. Changes:

1. **Wire the missing instrumentors** in `init()` (idempotent, guarded):
   - `HTTPXClientInstrumentor().instrument()`
   - `SQLAlchemyInstrumentor().instrument(engine=...)` at each engine build site.
2. **Default `FastAPIInstrumentor` on** for both the server and runner apps (currently
   gated behind `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION`; the remote-parent span patch in
   `telemetry.py:135` already handles MLflow's raw-span edge case).
3. **`service.name` per component** via `OTEL_SERVICE_NAME` (or a resource attribute set
   in `init()`) so Jaeger shows distinct services: `omni-host`, `omni-server`,
   `omni-runner`, `omni-harness`, `omni-tui`. This is what makes the
   service-dependency graph legible.
4. **Manual propagation helpers** (thin wrappers over `opentelemetry.propagate`) for the
   two control-frame websockets, co-located in `telemetry.py`:
   - `inject_into_frame(frame: dict) -> dict`
   - `extract_from_frame(frame: dict) -> Context`

`init()` already runs in every process entrypoint (`cli.py:3079`,
`runner/_entry.py:889`, `harnesses/_runner.py:364`), so every process gets a provider
uniformly — no reliance on the `opentelemetry-instrument` wrapper (which only wraps a
single process and would conflict with this programmatic setup; reserved for one-off
local probing).

---

## 8. Configuration

All standard OpenTelemetry env vars; nothing backend-specific in code.

| Variable | Dev value | Effect |
|---|---|---|
| `OMNIGENT_TELEMETRY_ENABLED` | `true` | **Master opt-in; off by default.** When unset/false, `init()` is a no-op and no instrumentor installs — zero telemetry cost. Required for any row below to take effect. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | Once opted in, enables OTLP export and selects the collector |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` (default) | `grpc` or `http/protobuf` |
| `OTEL_SERVICE_NAME` | per component | Service identity in Jaeger |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` (dev) | Always sample locally; ratio-based in prod |
| `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION` | `true` | Server/runner HTTP spans + extract |
| `OMNIGENT_OTEL_CAPTURE_CONTENT` | `true` (dev only) | Include payloads on spans; **off in prod** (PII) |

Telemetry is **opt-in**: nothing is instrumented and no spans are created unless
`OMNIGENT_TELEMETRY_ENABLED` is truthy, so a default install is never burdened with
telemetry it didn't ask for. Opt in first, then point `OTEL_EXPORTER_OTLP_ENDPOINT` at a
backend.

**Session correlation (`session.id`).** Every span that originates from a session is
tagged with the Omnigent session (conversation) id (`conv_…`) under the `session.id`
attribute: the FastAPI server span (parsed from the `/sessions/<conv_…>/` request path —
covers REST/SSE on **both** server and runner), the agent/LLM/tool/policy spans (from the
runner's `TracingContext`), the in-process `policy.evaluate` span, and `terminal.attach`.
This matters because an agent turn can root **its own** trace (the response-id-seeded
root) and the response path (the JSONL forwarder → SSE) is **decoupled from any request**,
so there is no shared request context there. `session.id` is therefore a **cross-trace
grouping key**: it lets the backend gather every span of a session even when they do not
share a `trace_id` — which raw W3C propagation alone cannot do across those decoupled
boundaries. The host control-plane frames carry no session id by design (a daemon control
action is not part of a user request) and rely on trace propagation to their parent span.

---

## 9. Payload capture: metadata always, bodies on request

By default a span records the **shape and metadata** of a message — route, method,
status, latency, the frame *kind*, the policy *decision* — but **not the body**. That is
the right default: bodies hold PII/secrets, the trace backend is not a payload store, and
the HTTP/WS auto-instrumentors never record bodies.

When an operator needs to see the literal contents flowing between services, set
`OMNIGENT_OTEL_CAPTURE_CONTENT=true`. This wires `should_capture_content()` (previously a
dormant flag) into the boundaries Omnigent controls:

- **Host-tunnel frames** — inbound on the consumer span (`consume_frame_span`) and
  outbound at `encode_host_frame`; recorded as `omnigent.message.payload`.
- **Session-updates WS frames** — outbound at `_send`, inbound at the `watch` consumer
  span.
- **Policy evaluation** — the content under evaluation, as `policy.content` on the
  `policy.evaluate` span.

Every captured body is **redacted** (`_redact_payload`: keys matching
`token`/`secret`/`password`/`authorization`/`credential`/`api_key` become `[redacted]`,
and `traceparent`/`tracestate` are dropped) and **length-capped** at
`_CONTENT_MAX_LEN` (4096 chars). Verified live: a `host.stat` frame span carries its full
result body and `policy.evaluate` carries the evaluated prompt, with a frame's
`binding_token` redacted.

**Intentionally NOT captured at the transport:** raw HTTP request/response bodies and SSE
event frames between server ↔ runner ↔ harness. Reading those in an instrumentation hook
would consume the stream and break SSE (the live chat transport), and that agent-turn
content is the **chat-log / durable event log** concern below — not the trace layer.

### Two layers: spans vs. durable event log

Spans are sampled and retention-limited — great for "follow one request," wrong for the
system-of-record. Questions like transcript reconstruction, fork/resume, and
local-vs-server divergence need a **complete, ordered, durable** record. Those are
written at the **same choke points** (SSE frame builder, the two control-frame
websockets) into an append-only event log keyed by
`(session_id, response_id|trace_id, seq, direction)`, carrying full payloads. This
design covers the **trace layer**; the durable event log is complementary and tracked
separately, but the instrumentation sites are shared so both can be added together.

---

## 10. Testing & verification

### 10.1 Local loop

1. Start Jaeger all-in-one (§3).
2. `export OMNIGENT_TELEMETRY_ENABLED=true` (master opt-in), then
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`,
   `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true`, `OMNIGENT_OTEL_CAPTURE_CONTENT=true`.
3. Start server + host daemon; run a turn from the TUI.
4. Open `http://localhost:16686`, pick service `omni-tui`, open the trace.

### 10.2 Acceptance criteria

- A single TUI turn produces **one trace** whose spans span services `omni-tui →
  omni-server → omni-runner → omni-harness`, plus child DB spans, under one `trace_id`.
- A `host.launch_runner` control frame produces a span on `omni-host` linked to the
  triggering server span (validates manual frame propagation).
- A native-harness policy check appears as an `omni-server` child span under the turn.
- The root span's trace ID equals `trace_id_from_response_id(response_id)`.

### 10.3 Automated test

An integration test (extending the existing telemetry suite) configures an
**in-memory span exporter** (`InMemorySpanExporter`), drives one end-to-end turn against
a local server+runner, and asserts: (a) all spans share one `trace_id`; (b) expected
service names and span kinds are present; (c) parent/child links cross each boundary
(no orphaned local roots). No Jaeger needed in CI — the in-memory exporter is the
official OTel test harness.

### 10.4 Verification results (local Jaeger, real turn)

A headless turn (`omnigent run --harness claude-sdk -p "…"`) against a local Jaeger
all-in-one produced the expected connected traces:

- **Agent turn — one trace, 54 spans across three processes**
  (`service.name` ∈ {`omni-server`, `omni-runner`, `omni-harness`}): server FastAPI +
  httpx spans, the tunnel-forwarded runner spans, and the harness turn-event spans all
  share one `trace_id`. Confirms httpx-inject → FastAPI-extract propagation across the
  server → runner (WS tunnel, verbatim headers) → harness (UDS) boundaries. The server's
  `policy.evaluate` span and the native policy HTTP hook appear inline.
- **Host control plane — one trace across `omni-host` + `omni-server`**: the daemon's
  `host.launch_runner` / `host.stat` consumer spans nest under the server's
  `POST /v1/hosts/{host_id}/runners` request — confirms the manual JSON-frame
  `traceparent` propagation (§6.4) with a real daemon.
- **Per-component `service.name`** distinguishes all four processes in the backend.

Browser propagation (§6.1) is validated by `web/src/lib/telemetry.test.ts` and the
production build; the server-side extraction it depends on is exercised by the live turn
above.

---

## 11. Rollout phases

**Phase 1 — auto-instrumentation + spine (low risk; ~5 of 7 boundaries):**
wire `HTTPXClientInstrumentor`; default FastAPI instrumentation on for server **and**
runner apps; add `SQLAlchemyInstrumentor`. Covers Web UI, TUI, Runner↔Server,
policy hook, DB. Verify against local Jaeger.

**Phase 2 — control-frame websockets (manual):** add `traceparent` to the host-tunnel
and session-updates frame envelopes; inject/extract at the mapped choke points; span per
frame kind. Covers Host Daemon ↔ Server and live session-list updates.

**Phase 3 — durable event log:** append-only event-of-record at the shared choke points;
replay/diff tooling for transcript reconstruction, fork, and resume.

---

## 12. Risks & open questions

- **MLflow raw-span handling.** The `_patch_mlflow_otel_remote_parent_spans` patch
  (`telemetry.py:135`) is required for auto-instrumented server spans with remote
  parents. Phase 1 must verify it holds when FastAPI instrumentation is on by default.
- **Runner app instrumentation.** Confirm the runner's ASGI app is FastAPI-instrumented,
  not only the server's (`app.py:1254`); the verbatim-header propagation across the
  tunnel depends on it.
- **Custom-transport client gap (resolved).** The process-wide `HTTPXClientInstrumentor`
  only patches httpx's *standard* transports. The server→runner client is built on the
  custom `WSTunnelTransport` (`runner/routing.py:_client_for_runner`), so it was invisible
  to the global hook: the forward injected no `traceparent` and the runner rooted a
  *disconnected* trace even though the hop is a synchronous RPC. Fixed by instrumenting
  that cached client instance directly via `telemetry.instrument_httpx_client` so the
  server→runner forward stays in the caller's trace. (The downstream turn — claude-native
  `tmux send-keys` + the log-polling forwarder — is a separate async boundary and is *not*
  covered by this; it remains its own trace, correlated by `conversation_id`.)
- **PII / secrets.** `OMNIGENT_OTEL_CAPTURE_CONTENT` must remain **off** outside dev;
  the durable event log (§9), not spans, is the right home for full payloads with proper
  access controls.
- **Sampling cost.** `always_on` is for dev/test only; production uses parent-based
  ratio sampling so the cross-boundary parent decision is honored consistently.
- **Browser-side propagation (implemented).** OTel web SDK with fetch/XHR
  instrumentation, opt-in via `VITE_OTEL_EXPORTER_OTLP_ENDPOINT`, exporting over OTLP/HTTP
  (`:4318`). Omnigent is same-origin with no `CORSMiddleware`, so same-origin `traceparent`
  propagation needs no server change; `propagateTraceHeaderCorsUrls` is scoped to the
  app's own origin. A future cross-origin deployment that introduces CORS must allow the
  `traceparent`/`tracestate` request headers (server and any reverse proxy) or the header
  is silently stripped.
</content>
</invoke>
