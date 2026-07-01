> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Observability / Distributed Tracing (and how this doc was verified)

> Authored by the orchestrator from `designs/OBSERVABILITY.md` + hands-on validation against a
> live local stack + Jaeger (PR #1617, merged to `main`). Anchors verified live.

## Role & boundaries
The tracing layer (`omnigent/runtime/telemetry.py`) gives **one connected trace per user action**
across every process, exported via **OTLP** to any backend (Jaeger locally). It records *structure*
(who called whom, over what transport, how long, decision) — **not** correctness, and **not** the
durable transcript (that's the conversation store; a complementary append-only event log is §9 of
the design, deferred). Opt-in: nothing instruments unless `OMNIGENT_TELEMETRY_ENABLED` is truthy.

## How it works (the model)
- **Standard W3C trace-context propagation**, no bespoke scheme. Inject a `traceparent` at every
  send site; extract+attach at every receive site. The deterministic `response_id → trace_id` seed
  (`trace_id_from_response_id`) is kept only so an operator can jump from a response id to its root
  trace; **all cross-boundary continuity is real propagation**.
- **`session.id` is the cross-trace grouping key.** A single conversation spans MANY `trace_id`s
  because the response path (JSONL forwarder → SSE) is **decoupled from any request** — there is no
  shared request context there. So `_SessionIdSpanProcessor` (`telemetry.py:~277`) stamps
  `session.id`=`conv_…` on *every* recording span, and you group a conversation by that tag, not by
  `trace_id`. **This is the single most important fact for reading Omnigent traces.**
- **Per-component `service.name`** set in each process's `telemetry.init(service_name)`:
  `omni-server`, `omni-runner`, `omni-harness`, `omni-host` (+ `omni-web` only if a browser sets
  `VITE_OTEL_EXPORTER_OTLP_ENDPOINT`).

## The five transport techniques (how the trace crosses each boundary)
| Boundary | Technique | Carrier |
|---|---|---|
| HTTP (REST, SSE handshake, native policy hook) | auto: FastAPI extract + HTTPX inject | HTTP headers |
| WS reverse-tunnel (runner↔server) | auto — tunnel forwards HTTP headers **verbatim**; httpx/FastAPI do the work | headers tunneled in the `request` frame |
| WS control frames (host tunnel, session-updates) | **manual** inject/extract into the JSON envelope | new `traceparent` field on the frame |
| Subprocess (harness/executor over UDS) | auto via tunneled headers | HTTP headers |
| Database (SQLAlchemy) | auto: `SQLAlchemyInstrumentor` (sink) | n/a |

Gotcha the PR fixed: the server→runner client uses a custom `WSTunnelTransport`, invisible to the
process-wide `HTTPXClientInstrumentor`; it's instrumented **explicitly** so the server→runner
forward stays in the caller's trace. The claude-native downstream (`tmux send-keys` + log-polling
forwarder) is a **separate async boundary** — its own trace, correlated only by `session.id`.

## Config (env)
`OMNIGENT_TELEMETRY_ENABLED=true` (master opt-in) · `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`
· `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true` · `OMNIGENT_OTEL_CAPTURE_CONTENT=true` (dev only —
records redacted, 4096-char-capped message bodies on host frames / session-updates / policy.content;
keys matching token/secret/password/authorization/credential/api_key → `[redacted]`).

## How THIS analysis was produced (reproducible recipe)
1. `docker run --rm -d --name jaeger -p 16686:16686 -p 4317:4317 -p 4318:4318 jaegertracing/all-in-one:latest`
2. Telemetry-enabled server on an isolated DB (the installed uv-tool `omnigent` is STALE — no OTel
   deps; run from the worktree `.venv`):
   `OMNIGENT_TELEMETRY_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true OMNIGENT_OTEL_CAPTURE_CONTENT=true .venv/bin/omni server --port 7777 --no-open --database-uri sqlite:///<scratch>/chat.db --artifact-location <scratch>/artifacts`
3. Drive a turn (same env; local runner tunnels to the server, both export):
   `.venv/bin/omni run <bundle-dir> -p "…" --server http://127.0.0.1:7777`
4. Extract by conv id with `scratchpad/jaeger_query.py` (groups by `session.id` across services):
   `summary <conv>` (services + **inter-component edges** + ops + captured payloads), `conv <conv> --denoise` (span tree), `recent <service>`.

## Reading-the-traces caveats (learned live)
- **Volume:** one trivial turn = ~14 traces / ~1300 spans, dominated by sqlite `connect`/`PRAGMA`
  and ASGI `http send`/`receive`. Always denoise to the inter-component + policy/tool/harness/frame
  spans (the `jaeger_query.py` `summary`/`--denoise` views do this).
- **One conv ≠ one trace.** Don't search by trace_id; search by the `session.id` tag.
- The **"inter-component edges"** view (parent_service→child_service : op) is the architecture
  signal — it's the empirical message catalog per component pair.

## CUJ corpus generated (evidence used throughout this doc)
`scratchpad/corpus/manifest.tsv` — convs for: claude-sdk turn (`conv_32db…`), claude-native turn
(`conv_94e6…`), subagent-spawn/MCP (`conv_fc47…`), policy-guarded (`conv_eb24…`), resume→new-runner
(`conv_32db…`), fork (`conv_151ad…`, server-only), webui-endpoint battery. codex sdk+native pending
a Databricks `oss` OAuth refresh (see Creds section) — codex behavior is code-verified meanwhile.
