# Omnigent — Telemetry & Distributed Tracing (master doc)

**Scope:** how observability works across Omnigent after PR #1617 (`feat(telemetry):
holistic distributed tracing across all components`). This doc is both the
architecture reference for the telemetry layer **and** the practical guide for
reading Omnigent traces during CUJ analysis. Design source: `designs/OBSERVABILITY.md`.
Code: `omnigent/runtime/telemetry.py`, `omnigent/inner/tracing.py`, plus per-boundary
instrumentation sites. Empirical evidence: local Jaeger, this analysis.

## 1. Overview — two complementary layers
Omnigent emits **two** kinds of spans that share one provider:

1. **Agent-plane / OpenInference spans** (`omnigent/inner/tracing.py`): semantic
   spans for the turn loop — `agent:<name>` (AGENT), `llm_call` (LLM),
   `tool:<name>` (TOOL), `policy:<name>` (GUARDRAIL). Built by a `TracingContext`
   that carries an explicit span stack (not contextvars), created by the runner's
   executor adapter for each turn. Attributes follow OpenInference
   (`openinference.span.kind`, `input.value`, `output.value`, `llm.model_name`,
   `gen_ai.usage.*`).
2. **Boundary / infra spans** (`omnigent/runtime/telemetry.py` + auto-instrumentors):
   FastAPI server spans (every HTTP route), HTTPX client spans (every outbound call),
   SQLAlchemy DB spans (every query), and **manual** spans for the JSON-frame
   websockets (host tunnel, session-updates) via `consume_frame_span` /
   `inject_trace_context` / `extract_trace_context`.

Both feed one global `TracerProvider` installed by `telemetry.init()`.

## 2. Initialization & configuration
`telemetry.init(service_name=...)` (`runtime/telemetry.py:1072`) runs in **every**
process entrypoint (cli, `runner/_entry.py`, `runtime/harnesses/_runner.py`). It is
**gated by a master opt-in** and is a complete no-op otherwise.

| Env var | Effect |
|---|---|
| `OMNIGENT_TELEMETRY_ENABLED` | **Master switch** (off by default). Unset → `init()` returns immediately; no provider, no instrumentation, no spans. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Enables OTLP export + selects the collector. When set, also default-enables FastAPI instrumentation, metrics, and logs export. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` (default) or `http/protobuf`. |
| `OTEL_SERVICE_NAME` | Per-component identity; each entrypoint passes its own (`omni-server`/`omni-runner`/`omni-harness`/`omni-host`; clients `omni-tui`/`omni-web`). A child overrides the inherited value. |
| `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION` | Force server/runner/harness FastAPI spans on/off (defaults on when an endpoint is set). |
| `OMNIGENT_OTEL_CAPTURE_CONTENT` | Include (redacted, 4 KB-capped) message bodies on frame/policy spans. Off by default (PII). |

What `init()` wires when enabled (`telemetry.py:1107-1141`): OTLP `BatchSpanProcessor`
+ a `_SessionIdSpanProcessor` on the `TracerProvider`; OTLP metrics + logs providers;
process-wide `HTTPXClientInstrumentor`. FastAPI instrumentation is installed by each
app factory via `instrument_fastapi_app()`. SQLAlchemy is instrumented per-engine at
creation (`db/utils.py` → `instrument_sqlalchemy_engine`).

## 3. The grouping key: `session.id` = conversation id
Because agent turns root their **own** trace (see §5), spans across a conversation do
**not** all share a trace id. The cross-trace grouping key is the **`session.id`**
attribute = the Omnigent conversation id (`conv_…`). It is stamped generically:
- `_SessionIdSpanProcessor.on_start` stamps every span from the active context
  (bound via `session_scope()` / `set_session_id()`), and
- `_fastapi_session_id_hook` parses it from the request path
  `/sessions/((?:agy_)?conv_[0-9a-f]+)` onto each server span — which also re-attaches
  the **decoupled JSONL forwarder** (it re-POSTs into `/events` under a fresh trace) to
  its session.

**This is the query key for CUJ analysis: filter/group traces by `session.id=<conv_…>`.**

## 4. Trace-id ↔ response-id
For request roots that own a response id (`resp_<32hex>`),
`trace_id_from_response_id()` (`telemetry.py:498`) reuses the hex as the W3C trace id,
and `trace_context_for_response()` injects a synthetic `traceparent` with a sentinel
parent span id (`SENTINEL_PARENT_SPAN_ID = 0x1000000000000001`). `start_agent_span`
detects the sentinel and exports the agent span as a true root (no `parentSpanId`).
Net effect: **you can jump from a response id to its trace with no lookup** — strip
`resp_` and search the trace id.

## 5. Why one action produces MANY traces (key reality)
Empirically, a single headless `claude-sdk` turn produced **21 distinct traces**
across `omni-server/runner/harness/host`; a 2-turn conversation
(`conv_b4f2faed…`) accumulated **26 traces / 2568 spans**. This is by design, not a bug:
- Each **agent turn roots its own trace** (response-id-seeded), decoupled from the
  client request that triggered it.
- The **response path is decoupled from any request**: a separate JSONL-polling
  forwarder tails the harness output and re-POSTs into `/events`; that POST roots a new
  trace (it carries no inbound request context), correlated only by `session.id`.
- **Host control-plane** frames (`host.launch_runner`, `host.stat`, …) and **server
  startup** root their own traces with **no** `session.id` (a daemon control action is
  not part of a user request). In the corpus these are the `<no-session>` traces
  (76 traces / 508 spans on `omni-host`+`omni-server`).

Implication for analysis (matches the team's Jun 30 standup note): **trace one single
action at a time**, then stitch every trace with the same `session.id`. The
`trace_tools.py conv <id>` helper does this.

## 6. Cross-boundary propagation (what carries the trace across processes)
| Boundary | Technique | Carrier |
|---|---|---|
| Client (TUI/web) → Server REST/SSE | FastAPI extract + HTTPX inject (auto) | HTTP headers |
| Server → Runner (WS reverse-tunnel) | tunnel forwards HTTP headers **verbatim**; the cached per-runner client is wrapped by `instrument_httpx_client` (custom `WSTunnelTransport` is invisible to the global hook) | tunneled HTTP headers |
| Runner → Harness/executor (UDS HTTP) | tunneled HTTP headers; `get_traceparent_env()` for subprocess env | HTTP headers / env |
| Host daemon ↔ Server (JSON control frames) | **manual** `inject_trace_context`/`consume_frame_span` | `traceparent` field on the JSON frame |
| Session-updates / terminal-attach WS | **manual** inject/extract into the JSON envelope | `traceparent` field |
| Server ↔ DB | `SQLAlchemyInstrumentor` (sink) | n/a |

**Verified locally:** one trace (`ecee1765…`, 324 spans) rooted at
`omni-server POST /v1/sessions/{session_id}/events` and crossed
**server → runner → harness**, with runner→server child calls for
`POST /policies/evaluate` and `GET /items` — i.e. W3C context propagates across every
synchronous boundary. The downstream native `tmux send-keys` + log-polling forwarder is
a separate async boundary (its own trace, correlated by `session.id`).

## 7. Service map (what each `service.name` emits)
- **omni-server** — FastAPI route spans (`POST /v1/sessions/{id}/events`, `GET
  /sessions/{id}`, `/items`, `/stream`, `/policies/evaluate`, `/mcp`), in-process
  `policy.evaluate` spans, and the dominant DB spans (`PRAGMA`/`SELECT`/`connect`/
  `INSERT`/`UPDATE` on `chat.db`).
- **omni-runner** — runner ASGI route spans, server→runner forwarded events, runner→
  server callbacks (`/policies/evaluate`, `/items`, `/mcp/execute`).
- **omni-harness** — the executor turn loop: OpenInference `agent:`/`llm_call`/`tool:`/
  `policy:` spans + harness→server `POST /events` (the forwarder path).
- **omni-host** — `host.launch_runner` / `host.stat` consumer spans (manual frame spans),
  nested under the server's `POST /v1/hosts/{id}/runners` when triggered by a request.
- **omni-tui / omni-web** — client-rooted spans when those clients run with telemetry
  (web SDK is opt-in via `VITE_OTEL_EXPORTER_OTLP_ENDPOINT`).

## 8. Content capture & redaction
With `OMNIGENT_OTEL_CAPTURE_CONTENT=true`, frame/policy spans carry the (redacted)
body under `omnigent.message.payload` / `policy.content`. `_redact_payload` masks keys
containing token/secret/password/authorization/credential/api_key and drops
`traceparent`/`tracestate`; bodies are capped at 4096 chars. Raw HTTP/SSE bodies between
server↔runner↔harness are intentionally **not** captured (reading them would break the
SSE stream); full transcript content is the durable-event-log concern, not the trace layer.

## 9. The local rig used for this analysis
- **Native Jaeger binary** (Docker is blocked on this Mac by an enforced org sign-in):
  `jaeger-all-in-one` v1.76, UI `:16686`, OTLP gRPC `:14317` (remapped off `:4317`,
  which the SSH tunnel occupies), OTLP HTTP `:14318`.
- **Drive a turn**: `omnigent run --harness claude-sdk -p "…"` from `<worktree>/.venv`
  with the env from §2 → boots an ephemeral server+runner+harness in-process and exports
  full traces. `--resume`/`--continue` for multi-turn; native/`--fork`/interrupt are
  REPL/server-only.
- **Extract**: `python3 scratchpad/trace_tools.py {recent|conv <id>|tree <traceid>|raw}`
  against `http://localhost:16686`.
- Jaeger all-in-one uses **in-memory storage** → traces are lost on restart; key convs
  are snapshotted under `scratchpad/corpus/`.

## 10. Gaps / caveats (telemetry layer)
- **DB span noise**: `PRAGMA`/`SELECT`/`connect` on `chat.db` dominate span counts
  (108 PRAGMA + 97 SELECT in one 324-span trace). Filter them when reading.
- **One-action-many-traces** makes "follow one request" non-obvious; `session.id` is the
  only reliable stitch across the decoupled forwarder/host/startup traces.
- **Live remote setup is dark**: the running server is remote (reached via the `:6767` tunnel)
  and its Jaeger UI (`:16686`) can't be tunneled (the SSH-tunnel manager's 32-forward cap), and
  the live local runner had telemetry **disabled** — hence the dedicated local rig.
- **codex / codex-native**: Databricks AI-gateway token expired (403) → no live codex
  spans in this corpus; covered from code + the §4 capability matrix.
- Per `OBSERVABILITY.md §12`: MLflow raw-span remote-parent patch, parent-based sampling
  in prod, and `OMNIGENT_OTEL_CAPTURE_CONTENT` must stay off outside dev (PII).
