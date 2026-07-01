# Omnigent — Master Architecture & CUJ Analysis

Deep, **code- and trace-grounded** map of Omnigent. Scope: **claude (sdk+native),
codex (sdk+native), polly** (custom agents). Built from a codebase pass (`file:line`
anchors), **live OpenTelemetry traces** (local Jaeger rig), and the existing
`designs/CUJ-MAP.md` / `CUJ-ANALYSIS.md` / `OBSERVABILITY.md`.

**Source-of-truth rule:** the running code is ground truth; traces validate/enrich it.
(`CUJ-ANALYSIS.md` line anchors had drifted thousands of lines — re-derived here.)

## Read in this order
1. **`architecture/overall-architecture.md`** — system topology, component inventory, the
   inter-component **channel matrix**, end-to-end request lifecycle, harness taxonomy, and
   **§9 verified corrections + live-trace findings**. Start here.
2. **`architecture/telemetry-tracing.md`** — how observability works **and how to read these
   traces** (the two span layers, `session.id` grouping, why one action = many traces).
3. **Per-component architecture** (`architecture/`):
   `server.md` · `runner.md` · `host.md` · `harness-inner.md` · `runtime-executor.md` ·
   `policies.md` · `web.md` · `tui-repl.md` · `auth-credentials.md` · `tools-omnibox.md`
4. **CUJ answers** (`cuj-answers/`):
   - **`_FOR-PASTE.md`** — answers to the Team Doc's *"How does claude code / codex / polly
     behave when…"* questions, in the doc's order, pasteable.
   - **`_LIVE-TRACE-FINDINGS.md`** — what the live traces revealed *beyond* the code.
   - per-domain: `server-api-state-streaming.md` · `runner-dispatch-mcp.md` ·
     `harness-behavior.md` · `executor-subagents-compaction-cache.md` · `host-channel.md` ·
     `policy-enforcement.md` · `web-client.md` · `tui-vs-web.md` · `credentials.md` ·
     `tools-mcp-shells.md`
5. **`CUJ-COVERAGE.md`** — every journey in `designs/CUJ-MAP.md` (§2.A–§2.H), each with an
   answer + `file:line` pointers + trace evidence.

## How the traces were produced (reproducible)
- **Native Jaeger binary** (Docker is org-locked on this Mac): UI `:16686`, OTLP gRPC `:14317`.
- **Persistent local stack**: `omnigent server --port 7777` + a local `omnigent host` (safe —
  per-server singleton, coexists with the remote-connected host), telemetry env → `:14317`.
- **Drive**: `omnigent run --server http://localhost:7777 …` (SDK) or REST
  (`POST /v1/sessions {agent_id,host_id,workspace}` then `POST /events
  {type:message,data:{role,content:[{type:input_text,text}]}}`); control via
  `{type:interrupt}` / `{type:compact}`; fork/switch via their endpoints.
- **Extract**: `python3 scratchpad/trace_tools.py {recent|conv <id>|tree <traceid>}` →
  Jaeger HTTP API, grouped by `session.id` (= conv id).

## Scope & caveats
- **codex / codex-native are live-blocked** (Databricks gateway creds; the host reports
  `codex: needs-auth`; OSS path blocked by a `DATABRICKS_BEARER`/PATH propagation gap in the
  runner spawn env). Covered from **code + the §4 capability matrix**, not fresh codex spans.
- The **TUI emits no spans today** (`telemetry.init("omni-tui")` is never called) — code-based.
- The **web UI** OTel is opt-in (`VITE_OTEL_EXPORTER_OTLP_ENDPOINT`) and was inactive — code-based.
