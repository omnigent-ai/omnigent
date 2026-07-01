# Omnigent — Master Architecture & CUJ Analysis

A deep, **code- and trace-grounded** map of Omnigent. Scope: **claude (sdk+native),
codex (sdk+native), polly**. Built from a codebase pass (`file:line` anchors) + **live
OpenTelemetry traces** (local Jaeger). Source-of-truth = the running code; traces
validate/enrich it. (`designs/CUJ-ANALYSIS.md`'s line anchors had drifted — re-derived here.)

## 📑 Table of contents

### Start here
| Doc | What it covers |
|---|---|
| [`architecture/overall-architecture.md`](architecture/overall-architecture.md) | System topology, component inventory, **inter-component channel matrix**, end-to-end request lifecycle, harness taxonomy, and §9 verified corrections + live-trace findings. |
| [`architecture/telemetry-tracing.md`](architecture/telemetry-tracing.md) | How observability works **and how to read these traces** (two span layers, `session.id` grouping, why one action = many traces). |

### Component architecture — [`architecture/`](architecture/)
| Doc | Covers |
|---|---|
| [`server.md`](architecture/server.md) | FastAPI server: `post_event` lifecycle, persist-before-forward, SSE vs durable, dedup, working-state, the full client→server route set, host/runner tunnels. |
| [`runner.md`](architecture/runner.md) | Runner dispatch + affinity, MCP routing (`/mcp`→`/mcp/execute`), request lifecycle, runner↔server WS reverse-tunnel + auth, resume dispatch. |
| [`host.md`](architecture/host.md) | Host daemon: JSON control-frame channel, runner launch/stop/exited, worktree/FS ops, spawn-env allowlist, host↔server auth. |
| [`harness-inner.md`](architecture/harness-inner.md) | SDK vs native harnesses, the per-harness capability matrix (§4), model/effort changes, elicitation hooks, default-model resolution, harness switching. |
| [`runtime-executor.md`](architecture/runtime-executor.md) | The executor turn loop, sub-agent spawning + depth, compaction (L1/L2/L3), inbox/async, agent cache, custom-agent storage. |
| [`policies.md`](architecture/policies.md) | Policy engine, creation (session/admin/spec), server vs runner enforcement, phases, the ASK/DENY elicitation flow, required native hooks. |
| [`web.md`](architecture/web.md) | Web UI: sidebar fetch, full client→server request/channel set, streaming↔durable reconciliation (dedup by `ctx.itemId`), working-state, reconnect. |
| [`tui-repl.md`](architecture/tui-repl.md) | TUI/REPL: `OmnigentClient` REST+SSE, TUI vs Web differences, slash commands, resume picker, event tape. |
| [`auth-credentials.md`](architecture/auth-credentials.md) | The three credential relationships + refresh paths, onboarding/provider selection, the native policy-hook token (PR #1439), caching TTLs. |
| [`tools-omnibox.md`](architecture/tools-omnibox.md) | The `sys_*` MCP surface, custom MCP, shells + working-dir resolution, timers, and OmniBox (the OS sandbox: isolation + egress + credential proxy). |

### CUJ answers — [`cuj-answers/`](cuj-answers/)
| Doc | Covers |
|---|---|
| [`_FOR-PASTE.md`](cuj-answers/_FOR-PASTE.md) | **The 26 team-doc "how does claude / codex / polly behave when…" questions**, in order — pasteable into the Team Doc. |
| [`_LIVE-TRACE-FINDINGS.md`](cuj-answers/_LIVE-TRACE-FINDINGS.md) | **11 CUJs traced live** — what the traces reveal beyond the code (fork, switch, interrupt, compaction, sub-agent, claude-native, disconnect, custom-MCP, timers, ASK/DENY, runner-binding). |
| [`server-api-state-streaming.md`](cuj-answers/server-api-state-streaming.md) | Request lifecycle, dedup, working-state, streaming vs durable, the full client→server API. |
| [`runner-dispatch-mcp.md`](cuj-answers/runner-dispatch-mcp.md) | Runner dispatch/affinity, MCP routing (custom vs `sys_*`), resume dispatch. |
| [`harness-behavior.md`](cuj-answers/harness-behavior.md) | Per-harness features, model/effort, config propagation, default resolution, switching. |
| [`executor-subagents-compaction-cache.md`](cuj-answers/executor-subagents-compaction-cache.md) | Executor role, sub-agents + depth, inbox, compaction, custom-agent storage, caching. |
| [`host-channel.md`](cuj-answers/host-channel.md) | Host daemon channel + data + runner launch. |
| [`policy-enforcement.md`](cuj-answers/policy-enforcement.md) | Policy creation + server/session enforcement + hooks + ASK/DENY. |
| [`web-client.md`](cuj-answers/web-client.md) | Sidebar fetch, full client→server set, streaming/durable reconciliation, close-page-return. |
| [`tui-vs-web.md`](cuj-answers/tui-vs-web.md) | TUI vs WebUI state + request-surface differences. |
| [`credentials.md`](cuj-answers/credentials.md) | Credential resolution + the three refresh paths + caching. |
| [`tools-mcp-shells.md`](cuj-answers/tools-mcp-shells.md) | `sys_*` surface, custom MCP, shells + cwd resolution, OmniBox, timers. |

### CUJ-MAP coverage
| Doc | Covers |
|---|---|
| [`CUJ-COVERAGE.md`](CUJ-COVERAGE.md) | **Every journey in `designs/CUJ-MAP.md` (§2.A–§2.H), 1:1** — 63 journeys, each with mechanism + `file:line` + ✅/⚠️ status + pointer to the deeper doc. |

### Tooling
| File | Purpose |
|---|---|
| [`trace_tools.py`](trace_tools.py) | Jaeger extraction helper — `python3 trace_tools.py {recent|conv <id>|tree <traceid>}`, grouped by `session.id`. |

## Suggested reading order
1. `architecture/overall-architecture.md` (the map) → 2. `architecture/telemetry-tracing.md` (how to read traces) →
3. the component doc(s) for your area → 4. `cuj-answers/_FOR-PASTE.md` for the specific behavior questions →
5. `CUJ-COVERAGE.md` to see any journey covered end-to-end.

## How the traces were produced (reproducible)
- **Native Jaeger binary** (Docker is org-locked on this Mac): UI `:16686`, OTLP gRPC `:14317`.
- **Persistent local stack**: `omnigent server --port 7777` + a local `omnigent host` (per-server
  singleton — coexists with a remote-connected host), telemetry env → `:14317`.
- **Drive**: `omnigent run --server http://localhost:7777 …` (SDK) or REST
  (`POST /v1/sessions {agent_id,host_id,workspace}` then `POST /events
  {type:message,data:{role,content:[{type:input_text,text}]}}`); control via
  `{type:interrupt}` / `{type:compact}`; fork/switch via their endpoints.
- **Extract**: `python3 trace_tools.py {recent|conv <id>|tree <traceid>}` → Jaeger HTTP API,
  grouped by `session.id` (= conversation id).

## Scope & caveats
- **codex / codex-native are code-only** here (Databricks-gateway creds; the host reports
  `codex: needs-auth`; the OSS path is blocked by a `DATABRICKS_BEARER`/PATH gap in the runner
  spawn env). Covered from **code + the §4 capability matrix**, not fresh codex spans.
- The **TUI emits no spans today** (`telemetry.init("omni-tui")` is never called) — code-based.
- The **web UI** OTel is opt-in (`VITE_OTEL_EXPORTER_OTLP_ENDPOINT`) and was inactive — code-based.
