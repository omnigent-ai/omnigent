# Omnigent — Master Architecture

> **Status:** trace-backed synthesis, 2026-06-30. **Source-of-truth rule:** the running **code**
> is ground truth. Every `path:line` in this doc was opened and confirmed against this checkout
> (`main` + distributed-tracing PR #1617) by a component subagent; claims that could not be
> confirmed are tagged `(unverified)`. Where this doc and the older `designs/CUJ-ANALYSIS.md`
> disagree, **this doc wins** — the divergences are listed in `CUJ-ANALYSIS.md`'s verification
> addendum.
>
> **How this was produced:** 10 parallel component subagents read the code AND a live trace corpus
> (real turns driven against a local server → Jaeger; see §9). Trace evidence is cited as
> `conv_…` ids throughout. Reproduce/extend with `scratchpad/jaeger_query.py` and the recipe in §9.
>
> **Scope:** Claude (sdk + native), Codex (sdk + native), Polly / custom agents. Other harnesses
> (cursor, pi, goose, hermes, antigravity, kimi, qwen, kiro, opencode, copilot, openai-agents)
> exist but are out of scope here.
>
> **⚠️ Round-2 erratum (2026-06-30):** a follow-up pass **drove the code-only CUJs live** and
> overturned two claims in this doc — **(R1) timers DO work**, and **(R2) an offline runner on a
> live host DOES recover** via host-relaunch. The full round-2 corrections + the broader CONFIRM set
> are in **§10**; any inline claim about `sys_timer_*` being non-functional (§7-Tools) or runner
> "no failover" (§7-Runner) is **superseded by §10**.

## Document map — all deliverable files

> **New here? The index is [`architecture/README.md`](architecture/README.md).** The four deliverable categories:

| Doc | What |
|---|---|
| **`ARCHITECTURE.md`** *(this file)* | **Overall architecture** — read top-to-bottom, or jump via the TOC below. |
| [`architecture/`](architecture/) | **Component architecture** — 11 per-component deep-dives + index: [server](architecture/server.md) · [runner](architecture/runner.md) · [host](architecture/host.md) · [executor-harness](architecture/executor-harness.md) · [policy](architecture/policy.md) · [tools-mcp-sandbox](architecture/tools-mcp-sandbox.md) · [agents-subagents-routing](architecture/agents-subagents-routing.md) · [web](architecture/web.md) · [tui-repl](architecture/tui-repl.md) · [creds-auth-onboarding](architecture/creds-auth-onboarding.md) · [observability](architecture/observability.md) |
| [`CUJ-MAP.md`](CUJ-MAP.md) | **CUJs** — the inventory (list of journeys). |
| [`CUJ-ANALYSIS.md`](CUJ-ANALYSIS.md) | **CUJs** — per-CUJ mechanisms + §7 (round-1) & §8 (round-2) verification. |
| [`STABILITY-CUJ-ANSWERS.md`](STABILITY-CUJ-ANSWERS.md) | **Gdoc answers** — paste-ready "Stability & Reliability" answers. |

## Table of contents (this file)
1. [System overview](#1-system-overview)
2. [Process topology & channels](#2-process-topology--channels)
3. [End-to-end request lifecycle](#3-end-to-end-request-lifecycle)
4. [Inter-component message & channel catalog](#4-inter-component-message--channel-catalog)
5. [Per-harness capability matrix](#5-per-harness-capability-matrix)
6. [Cross-cutting invariants & reliability gaps](#6-cross-cutting-invariants--reliability-gaps)
7. [Component deep-dives](#7-component-deep-dives) — server · runner · host · executor+harnesses · policy · tools/MCP/sandbox · agents/subagents/routing · web · tui/repl · creds/auth
8. [Observability & how to read traces](#8-observability--how-to-read-traces)
9. [Trace corpus index](#9-trace-corpus-index)
10. [Round-2 live-driving corrections](#10-round-2-live-driving-verification-2026-06-30)

---

## 1. System overview

Omnigent is a **distributed, multi-process agent platform**. A user (via the Web UI or the
TUI/REPL) talks to a **server** that owns all durable state; the server dispatches each
conversation's turns to a **runner** process that hosts the actual agent **harness/executor**;
an optional **host daemon** launches runners on a machine for managed/sandboxed execution.

Five processes, each its own OTel `service.name`:

| Process | `service.name` | Role |
|---|---|---|
| Server | `omni-server` | FastAPI control plane + `ConversationStore` + DB. **Source of truth.** Persist-before-forward, SSE live-tail, policy gate, in-memory live state. |
| Runner | `omni-runner` | One ASGI app **per conversation**. Hosts the executor + MCP pool + tool execution + system resources (shells/terminals/sandbox). Reaches the server only over a WS reverse-tunnel. |
| Harness/executor | `omni-harness` | The per-turn event generator. **Two families** (below). |
| Host daemon | `omni-host` | Control-plane-only daemon that launches/manages runners on a host. Never sees a turn. |
| Web | `omni-web` | Browser SPA (only emits its own trace span when `VITE_OTEL_…` is set). |
| TUI/REPL | `omni-tui` | Pure HTTP client of the server. Owns no runner, persists nothing. |

**The single most important split — two harness families** (it explains most behavioral
differences):

- **SDK harnesses** (`claude-sdk`, `codex`/codex-sdk; **Polly** runs here too) — an **in-process
  agent loop**. Omnigent owns the prompt, the tool set, and the turn loop; the transcript is 100%
  Omnigent's; the runner **drives** the harness (`runner → harness POST /events`). `handles_tools_internally=True`, `supports_streaming=True`.
- **Native harnesses** (`claude-native`, `codex-native`) — Omnigent drives a **resident vendor CLI**
  (Claude Code / Codex) in a **tmux pane** (claude) or over a vendor RPC (codex), injects only the
  latest user message, and a **forwarder mirrors the vendor's transcript back** as `external_*`
  events. The *vendor* owns the system prompt + tool set; the transcript lives in the vendor store
  and is mirrored. `supports_streaming=False`; the forwarder is the **sole history writer**
  (server single-writer bypass).

This inversion is visible in the trace corpus: an SDK turn (`conv_32db…`) shows
`runner→harness POST /events ×3` driving the turn; the native turn (`conv_94e6…`) shows
`runner→harness ×1` (one inject) but `runner→server POST /events ×14` (the forwarder posting
`external_*` back) + `GET /labels ×7` (reading the `bridge_id`).

---

## 2. Process topology & channels

```
        ┌─────────────────────┐                  ┌──────────────────────────────┐
        │  Browser (omni-web) │                  │  TUI / REPL (omni-tui)        │
        │  SPA, React         │                  │  pure HTTP client             │
        └──────────┬──────────┘                  └───────────────┬──────────────┘
   REST + SSE(/stream) + WS(/sessions/updates)        REST + SSE(/stream) ONLY
   + HTTP poll(/health)                                (no WS sidebar; rail polled)
                   │                                                │
                   ▼                                                ▼
   ┌───────────────────────────────────────────────────────────────────────────────────┐
   │  omni-server  — FastAPI control plane + ConversationStore + DB (SOURCE OF TRUTH)     │
   │  • persist-before-forward  • SSE live-tail (snapshot + [DONE])                       │
   │  • in-process policy.evaluate  • in-memory status/presence/fences (SINGLE-REPLICA)   │
   │  NB: runner/routing.py + WSTunnelTransport execute HERE, in the server process.      │
   └───┬───────────────────────────────────────────────┬───────────────────────────────┘
       │ WS reverse-tunnel (runner = client;            │ WS JSON control frames
       │ HTTP framed + headers forwarded VERBATIM)       │ (16 HostFrameKinds; per-request_id Futures)
       ▼                                                 ▼
   ┌───────────────────────────────┐              ┌───────────────────────────────────┐
   │  omni-runner (1 / conversation)│              │  omni-host (control-plane daemon)   │
   │  ASGI app: executor + MCP pool │              │  launches runners; never sees a turn│
   │  + tool exec + shells/sandbox  │              └───────────────┬─────────────────────┘
   └───────────────┬────────────────┘                 env-based one-way spawn (allowlist)
       UDS / in-proc │                                                │
                     ▼                                                ▼
   ┌──────────────────────────────────────────────┐         (spawns an omni-runner)
   │  omni-harness                                  │
   │  SDK: in-proc loop (Omnigent owns prompt/tools)│
   │  native: vendor CLI in tmux + forwarder mirror │
   └──────────────────────────────────────────────┘
        (native only) tmux send-keys + JSONL log-poll forwarder
        = a SEPARATE async boundary / trace, correlated only by session.id
```

**Channel legend** (the "what channel" answer):

| Channel | Between | Carries | Notes |
|---|---|---|---|
| **REST** | client → server; runner ↔ server (tunneled) | session CRUD, events, snapshots, policy eval, MCP dispatch | FastAPI; `traceparent` auto-propagated |
| **SSE** (`GET /sessions/{id}/stream`) | server → client | `response.*` deltas + `session.*` lifecycle | live-tail only, **no replay buffer**; ends `[DONE]` |
| **WS `/sessions/updates`** | server → **web only** | sidebar watch-set snapshot + changed/removed deltas + heartbeat | TUI does **not** use this |
| **WS reverse-tunnel** (`/v1/runners/{id}/tunnel`) | runner (client) ↔ server | **all** server↔runner HTTP, framed; headers verbatim | runner dials out; 30s ping / 3-miss death |
| **WS control frames** (`/v1/hosts/{id}/tunnel`) | host ↔ server | JSON `kind`-discriminated frames (launch/stop/stat/fs ops) | NOT HTTP; per-`request_id` Future multiplexing |
| **UDS (real HTTP)** | runner → harness | turn input + events | harness is a subprocess reached via `httpx(uds=…)` → uvicorn; the *executor object* is in-process WITHIN that harness |
| **tmux send-keys + log-poll** | runner/forwarder ↔ native vendor CLI | keystroke inject + JSONL transcript mirror | native only; async, off the request trace |

---

## 3. End-to-end request lifecycle

**Canonical SDK turn** (claude-sdk, evidenced by `conv_32db…`; anchors in §7-Server/§7-Runner):

```
1. CLIENT  POST /v1/sessions            → server INSERTs conversation+agent+permissions (DB)
2. CLIENT  GET  /v1/sessions/{id}/stream → server opens SSE live-tail (snapshot then live)
3. CLIENT  POST /v1/sessions/{id}/events {type:message}
   └─ SERVER: append item to ConversationStore  ── PERSIST-BEFORE-FORWARD (S:8540)
              → POST runner /events  (over WS tunnel, S:8697)
              → publish session.input.consumed (client dedup anchor)
4. RUNNER  pulls context from server (over tunnel): GET /agent/contents, GET /items, GET /skills
   └─ instantiates executor; drives the harness SUBPROCESS via ONE long-lived streaming
      POST /v1/sessions/{id}/events — real HTTP over UDS (httpx → uvicorn), NOT in-process
5. POLICY  in-process policy.evaluate spans on server: PHASE_REQUEST → LLM_REQUEST → LLM_RESPONSE
6. STREAM  executor yields ExecutorEvents → harness streams them back as SSE frames on the
           RESPONSE BODY of step-4's POST (the harness never calls the runner) → runner relays
           → server → SSE response.* deltas to client
   └─ DURABILITY: only OutputItemDoneEvent persists as a conversation item;
                  reasoning + all response.* deltas are SSE-only (never persisted)
7. TOOLS   (if any) harness emits action_required in its stream → runner → server POST /mcp
                    (policy gate: TOOL_CALL, then TOOL_RESULT) → server → runner POST /mcp/execute
                    (the actual execution) → runner POSTs {tool_result} back to the harness
                    /events (a SEPARATE short POST, not PATCH) so the loop continues
8. DONE    TurnComplete → status idle (in-memory _session_status_cache) → SSE [DONE] on stream end
```

**Native turn divergence** (claude-native, `conv_94e6…`): step 3's server append happens, **but the
forwarder is the sole history writer** (single-writer bypass, S:8735); step 4–6 are replaced by:
runner injects the one user message into the tmux vendor CLI (`runner→harness ×1`), the **forwarder**
watches the vendor JSONL and posts `external_*` events back to the server (`runner→server POST
/events ×14`) + reads the `bridge_id` label (`GET /labels ×7`). Policy phases observed are
REQUEST/TOOL_CALL/TOOL_RESULT (via the native PreToolUse/UserPromptSubmit hook), not LLM_REQUEST/RESPONSE.

**Resume** (`conv_32db…` resumed): the server loads **no transcript**; it re-binds a fresh runner
(`PATCH /sessions/{id} {runner_id}` — **last-writer-wins, not CAS**) and the **new runner pulls the
full transcript** itself via paginated `GET /items` + `GET /agent/contents`. **Fork** (`conv_151ad…`):
a **synchronous server-only deep copy** (new top-level conversation, fresh item ids, lineage only via
the `omnigent.fork.source_id` label) — **no runner is spawned** until the fork runs a turn (the fork
trace had zero inter-component edges).

---

## 4. Inter-component message & channel catalog

Empirical, from the trace corpus (every edge below was observed; `→` = caller to callee). This is
the "what data flows between each pair of components, over what channel" answer.

### client (web/TUI) → server — REST + SSE (+ WS for web)
| Op | Purpose | Durable? |
|---|---|---|
| `POST /v1/sessions` (multipart) | create session + upload ephemeral agent bundle | yes (conversation/agent rows) |
| `GET /v1/sessions/{id}` / `…?include_items=false` (slim) | snapshot / reconnect | n/a |
| `GET /v1/sessions/{id}/items` | windowed history page (replay) | n/a |
| `GET /v1/sessions/{id}/stream` (SSE) | live-tail output | streaming only |
| `POST /v1/sessions/{id}/events` | send message / interrupt / approval / compact | message persists; control events do not |
| `PATCH /v1/sessions/{id}` | model/effort/archived/runner-bind | yes |
| `POST /v1/sessions/{src}/fork` | fork (server-only deep copy) | yes |
| `POST /v1/sessions/{id}/elicitations/{eid}/resolve` | approve/deny an ASK | applies withheld writes |
| `GET /v1/sessions/{id}/child_sessions`, `/agent` | subagent rail, agent info | n/a |
| `WS /v1/sessions/updates` (**web only**) | sidebar watch-set + deltas | n/a |
| `GET /health?session_ids=` (web poll), `GET /api/version`, `GET /v1/info`, `/v1/me`, `/v1/policy-registry`, `/v1/sessions/projects` | liveness, capabilities, sidebar | n/a |

### server ↔ runner — over the WS reverse-tunnel (framed HTTP)
| Direction · Op | Purpose |
|---|---|
| server→runner `POST /v1/sessions` | launch/bind the runner for a conversation |
| server→runner `POST …/events` | forward a user input event into the turn |
| server→runner `GET …/resources/terminals`, `…/skills`, `…/{id}` | live runner state for snapshots |
| server→runner `POST …/mcp/execute` | execute a tool (after the server-side policy gate) |
| runner→server `GET …/agent/contents`, `…/items` | pull agent bundle + transcript to build context |
| runner→server `GET …/labels` (**native**) | read `bridge_id` / instance labels |
| runner→server `POST …/events` (**native**, `external_*`) | forwarder mirrors vendor transcript back |
| runner→server `POST …/policies/evaluate` | escalate an ASK / native-hook policy eval |
| runner→server `POST …/mcp` | route a tool call to the server policy gate |
| runner→server `PATCH …/{id}`, `GET …/child_sessions`, `GET /api/version` | bind/model updates, subagent enumeration, version handshake |

### host ↔ server — WS JSON control frames (NOT HTTP)
`host.hello`, `host.launch_runner`, `host.stop_runner`, `host.runner_exited`, `host.stat`,
`host.list_dir`, `host.create_dir`, `host.worktree`, … (16 `HostFrameKind`s) — multiplexed by
`request_id` via `asyncio.Future`s; each frame carries a `traceparent` (PR #1617).

### in-process
`policy.evaluate` (server, every phase) · DB queries (SQLAlchemy spans) · runner→harness over UDS.

**Durable vs streaming (the reasoning question):** only `OutputItemDoneEvent` is durable
(`schemas.py:3724`); `response.*` text/reasoning deltas, `session.*` lifecycle, and turn-lifecycle
events are **SSE-only**. **Reasoning is streamed but never persisted as an item** (it is re-derived
on SDK, mirrored on native). Assistant text is buffered in the relay and flushed as one `message`
item at tool boundaries / turn end.

---

## 5. Per-harness capability matrix

Code-verified cell-by-cell against each `inner/*_executor.py` + native bridge/forwarder (anchors in
§7-Executor). **Read `interrupt — product` as "does the web Stop button stop the running turn,"**
which is **not** the same as "the executor's `interrupt_session()` method exists" (a prior analysis
conflated the two — corrected here).

| Harness | interrupt (exec method) | interrupt (product Stop) | queue (`supports_live_message_queue`) | tool-boundary interrupt | subagents | reasoning-effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|---|---|
| **claude-sdk** | ✅ `interrupt()`+close (`:1477`) | ✅ | ✅ (`:1614`) | ✅ (`:1617`, the **only** harness) | ✅ (`sys_session_*` MCP) | ✅ {low,med,high,xhigh,max} | ✅ (`_can_use_tool`→elicitation `:1633`) | ✅ live `set_model` (`:1422`) |
| **codex-sdk** | ✅ `interrupt_turn()`+close (`:2243`) | ✅ | ✅ (`:2240`) | ❌ base False | ⚠️ subprocess `CODEX_HOME` isolation | ✅ {none,minimal,low,med,high,xhigh} | ⚠️ executor base ❌; forwarder may handle | ⚠️ **rebuilds thread** (`:2303`, loses state) |
| **claude-native** | ❌ base False (no-op) | ✅ via bridge `inject_interrupt` Escape (`bridge:2530`, called `app.py:10518`) | ✅ tmux inject | ❌ base False | ✅ vendor Task → `external_subagent_start` | ⚠️ vendor-only (`/effort` mirrored, next turn) | ✅ via hook/policy + vendor UI | ⚠️ **vendor-only, next turn** (config ignored `:123`) |
| **codex-native** | ✅ `turn/interrupt` RPC (`:116`) | ✅ | ✅ `turn/steer` (`:68`) | ❌ base False | ⚠️ subprocess isolation | ✅ {…openai} via `thread/settings/update` (`:266`) | ✅ forwarder hook | ✅ `thread/settings/update` (`:237`) |

**Polly / custom agents** have no row — they run on a chosen harness (typically claude-sdk) and
inherit it. Notes: only **claude-sdk** overrides `supports_tool_boundary_interrupt` — "queue ✅" on
the others means input is accepted but applied at turn (not tool) boundaries. The two SDKs differ on
mid-session model: claude-sdk mutates the **live** client (`set_model`, keeps state); codex-sdk does
a full app-server **thread teardown+rebuild** keyed on `(model,prompt,cwd,tools)`. Reasoning-effort
source of truth: `omnigent/reasoning_effort.py`.

---

## 6. Cross-cutting invariants & reliability gaps

Aggregated, code-confirmed across the component passes. ⚠️ = failure branch / live gap.

**Invariants that hold:**
- **Persist-before-forward** — input is appended to the store before it is forwarded to the runner
  (server S:8540 → S:8697); `session.input.consumed` fires only after the forward.
- **Runner affinity — pinned, no rebalancing, but host-relaunch recovery exists** *(corrected, §10 R2)*.
  `conversations.runner_id` pins a conversation to one runner; there is no load-rebalancing across
  runners. But an offline runner on a **live host is not a dead end**: the **message path**
  (`POST /events`) relaunches it via `host.launch_runner` (`_launch_runner_on_host` `sessions.py:6035`,
  "host-relaunch optimism" `app.py:1590`) and **LWW-rebinds** (`replace_runner_id`), returning
  `{queued:true}` so the turn runs. `RUNNER_UNAVAILABLE` (503, `routing.py:175`) is raised only on the
  **resource path** (`GET /resources/*`, no relaunch) or when the **host itself** is dead. Initial
  bind is a real CAS (`set_runner_id`, `WHERE runner_id IS NULL`); resume/relaunch rebind is LWW.
- **Policy reach** — REQUEST **and** TOOL_CALL fail-**CLOSED**; TOOL_RESULT/LLM_* fail-**OPEN**.
  DENY short-circuits, ASK accumulates.
- **Dedup** — there is **NO server-side dedup** (only `(conversation_id, position)` is unique).
  Dedup is **client-side** (web: by `ctx.itemId`; TUI: crude counters) **+ runner cold-cache** (by
  `persisted_item_id`). `session.input.consumed` is a *client* anchor, not a server dedup set.
- **Transcript on resume** — server loads none; the re-bound runner pulls the **full** transcript
  via paginated `GET /items` + `/agent/contents`.

**Reliability gaps (⚠️) confirmed in code:**
- **In-memory, single-replica live state** — status/presence/read-state/interrupt-fences live only
  in server memory (`_session_status_cache`); a restart or a second replica desyncs all of it.
- **No spawn-time subagent depth cap** — `_MAX_SUBAGENT_TREE_DEPTH=3` is **display-only**; runtime
  clone-spawns-clone is explicitly possible → runaway-recursion risk.
- **`sys_timer_*` WORK** *(corrected, §10 R1 — was wrongly listed here as non-functional)*: the runner
  intercepts at `runner/tool_dispatch.py:4133` → `_execute_timer_set` (real asyncio `_timer_loop`),
  returns `scheduled`, fires mid-turn. The `timer.py:220` `NotImplementedError` stub is **dead code on
  the runner path**. (Separately, **`sys_cancel_task` remains a no-op** — the tasks table was dropped.)
- **Native out-of-turn `serve-mcp` runs `sys_os_*` UNGATED** — the workspace-tool path bypasses the
  policy gate that the in-turn relay enforces.
- **`HelloFrame.harnesses` is hardcoded** and omits `codex-native` (+ others) → latent
  `RUNNER_CAPABILITY_MISMATCH` since dispatch gates on it.
- **Corporate-proxy gap (#1022) is two-layer** — neither the host-daemon env nor the runner-spawn
  allowlist carries `HTTP(S)_PROXY`/`NO_PROXY`.
- **`credential_proxy` trust-boundary (#1542)** — parent-side `subprocess.run(..., shell=True)` +
  arbitrary file reads on a "trusted-spec-only" assumption.

**Resolved since the last analysis (NOT gaps anymore):**
- The native policy-hook **fail-closed-after-~1h token-expiry bug is FIXED on `main`** for in-scope
  harnesses (claude/codex re-mint the hook token on 401 *or* Apps 302→`/oidc/` via
  `post_evaluate_with_retry(reauth=…)`); only `pi_native` (Node, out of scope) still has it.

---

## 7. Component deep-dives

Each subsection below was written by a component subagent against this checkout, with `path:line`
anchors verified and trace evidence cited. They retain their own "answers to the team's questions"
and "corrections to CUJ-ANALYSIS" subsections.

---

### Server (FastAPI control plane + conversation store + DB)

Anchors are `path:line` in `/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`
(`main` + telemetry PR #1617). All opened & confirmed unless tagged `(unverified)`.
`S` = `omnigent/server/routes/sessions.py` (20629 lines).

#### 1. Role & boundaries

The server (`omni-server`) is the **source of truth and the only writer of durable
conversation state**. It owns:
- **Conversation store + DB** (`omnigent/stores/conversation_store/`, `omnigent/db/`):
  conversations, conversation_items, conversation_labels, session_permissions.
- **The REST/SSE/WS surface** clients talk to (55 OpenAPI paths + 2 WS + 1 SSE).
- **Request lifecycle**: validate → policy → **persist-before-forward** → forward to the
  bound runner → relay the runner's SSE back, persisting durable items as they stream.
- **Runner/host tunnel termination**: runners and hosts dial *in* over a WS tunnel; the
  server tunnels HTTP-over-WS to them (`WSTunnelTransport`). It does NOT open sockets to
  runners.
- **Affinity**: `conversations.runner_id` (the bound runner) via `PATCH /sessions/{id}`.
- **Ephemeral live state**: `_session_status_cache`, presence, pending-elicitation counts,
  interrupt fences, read-state — all in-memory, single-replica (§7).

It does **NOT**: run the LLM, hold the harness/transcript-of-record for native harnesses
(the native CLI's own session file is authoritative; the server mirrors via the
forwarder), sandbox/exec (OmniBox lives runner-side), or persist most stream events
(only `OutputItemDoneEvent` items are durable — §6).

#### 2. Key files & entrypoints (verified)

- `S:18150 post_event` — `POST /v1/sessions/{id}/events`, the dispatch hub (~1000 lines).
- `S:8495 _forward_event_to_runner` — **invariant I1**: persist (`8540`) then forward (`8697`),
  `session.input.consumed` published AFTER forward succeeds (`8704`).
- `S:8735 _dispatch_session_event_to_runner` — native-message single-writer bypass vs persist.
- `S:9371 _relay_runner_stream` — subscribes runner SSE, persists durable items, republishes
  to local `session_stream` (`9810`); exits → `_publish_status(failed, runner_disconnected)` (`9823`).
- `S:8930 _extract_persistent_item_from_sse` — which SSE events become durable items.
- `S:11229 _stream_live_events` — client SSE generator: ready heartbeat, interval heartbeat,
  `[DONE]` in `finally` (`11341`), presence connect/disconnect, `ServerStreamEvent` validation.
- `S:19190 stream_session` — `GET /v1/sessions/{id}/stream` (live-tail only, snapshot-on-connect).
- `S:14531 session_updates` — `WS /sessions/updates` (sidebar watch/snapshot/changed/removed).
- `S:14132 get_session` → `_get_session_snapshot` — durable snapshot (`_build_session_response` `S:2352`).
- `S:13688 create_session`; `S:15189 fork_session`; `S:15423 switch_session_agent`;
  `S:14800 update_session` (runner-binding LWW); `S:19363 delete_session`.
- `S:5343 _publish_status` (the single status funnel); `S:5777 _get_runner_client`.
- `omnigent/server/routes/runner_tunnel.py:277 tunnel` — `WS /v1/runners/{id}/tunnel`.
- `omnigent/server/routes/host_tunnel.py`, `terminal_attach.py`, `session_policies.py`.
- `omnigent/server/schemas.py:3724 ServerStreamEvent` — the SSE discriminated union (§6).
- DB/store (from sub-agent map): `Conversation` dataclass `omnigent/entities/conversation.py:185`;
  `ConversationItem` `…:699`; ids `omnigent/db/utils.py:619/639`; tables `omnigent/db/db_models.py:236/452/513/195`.

#### 3. Internal model

**Durable (DB):**
- `conversations` (`db_models.py:236`): `id` (`conv_<hex>`, `db/utils.py:619`), `agent_id`,
  `runner_id` (nullable, no FK — the affinity pin), `host_id`, `parent_conversation_id`,
  `root_conversation_id` (NOT NULL), `kind` (`default`/`sub_agent`), `title`, `model_override`,
  `reasoning_effort`, `cost_control_mode_override`, `harness_override`, `sub_agent_name`,
  `external_session_id` (native CLI's own session id), `next_position`, `session_state`,
  `session_usage`, `workspace`, `git_branch`, `archived` (Boolean column, **not** a label).
- `conversation_items` (`db_models.py:452`): `id` (`msg_`/`fc_`/`fco_`/`err_`/… + hex,
  `db/utils.py:639`), `response_id` (turn group), `position`, `type`, `data`, `created_by`,
  `search_text`. **Unique only on `(conversation_id, position)`** (`db_models.py:497`) — there is
  **no uniqueness/dedup constraint on item id or any client-supplied id**.
- `conversation_labels` (`db_models.py:513`): PK `(conversation_id, key)`, upsert-only,
  `value` String(256). Carries runner-owned/native overlays:
  `omnigent.last_task_error_{code,message}` (`S:502`), fork directives
  (`omnigent.fork.source_id`, `FORK_CARRY_HISTORY`), codex collaboration mode, etc.
- `session_permissions` (`db_models.py:195`): PK `(user_id, conversation_id)`, `level`
  1=read/2=edit/3=manage/4=owner.
- **No `tasks` table** (dropped, migration `b9c1d2e3f4a5`). Status is NOT durable; it is derived
  from the relay-fed cache (§6).

**Ephemeral (in-process, lost on restart, single-replica):**
- `_session_status_cache: dict[str,str]` (`S:837`) — idle/running/waiting/failed; the *only*
  source of "busy" (`S:12701`). Rederivable from the runner.
- `_session_background_task_count_cache` (`S:857`) — sticky bg-shell tally; refreshes only at a
  turn boundary (documented KNOWN LIMITATION `S:847`).
- `_interrupt_fenced_sessions: set[str]` (`S:931`) — sessions whose Stopped turn's trailing
  `response.*` the relay must drop.
- `_read_last_seen` / `_read_explicit_unread` (`S:869`) — per-user read state, **no durable
  source**, resets on restart (accepted tradeoff `S:863`).
- `presence` (per-tree viewer set, `omnigent/server/presence.py`), `pending_elicitations`
  (sidebar badge counts).

#### 4. Inter-component channels (every edge in/out)

```
        REST(JSON) + SSE                  WS tunnel (HTTP-over-WS, runner dials in)
client ───────────────► SERVER ◄════════════════════════════════════ RUNNER
   ▲   GET .../stream (SSE)   │  POST /v1/sessions (notify+turn-start)   │
   │   WS /sessions/updates   │  GET .../items, .../agent/contents       │
   └───────────────────────────  POST .../events (interrupt/stop/result) │
                              │  GET .../resources/terminals, .../skills │
                              │  POST .../mcp/execute                    │
                              └──► HOST (WS tunnel: POST /hosts/{id}/runners launch)
RUNNER ──callbacks──► SERVER: POST .../events (external_*, transcript mirror, status, usage),
   POST .../policies/evaluate, GET .../items, PATCH .../{id} (bind/labels), POST .../mcp
```

| Peer | Transport | Direction / ops | Durable? |
|---|---|---|---|
| **Client↔Server** | REST JSON | `POST /events` (input/interrupt/approval/stop/compact), session CRUD, fork, switch-agent, snapshot `GET /sessions/{id}`, items, policies | input persisted (I1); rest as noted |
| Client←Server | **SSE** `GET /sessions/{id}/stream` | `response.*` deltas + `session.*` lifecycle; live-tail only, no replay | only `output_item.done` durable |
| Client↔Server | **WS** `/sessions/updates` | client→ `{watch, session_ids}`; server→ `snapshot`/`changed`/`removed`/`heartbeat` | pull-diff of durable list rows |
| Client↔Server | **WS** `terminal_attach.py` | tmux PTY attach for native terminal sessions | n/a (transient bytes) |
| **Server↔Runner** | **WS tunnel** `/v1/runners/{id}/tunnel` (`runner_tunnel.py:277`) | HTTP-over-WS both ways; runner dials in, server `WSTunnelTransport` issues HTTP | tunnel for all server↔runner HTTP |
| Server→Runner | tunneled REST | `POST /v1/sessions` (notify+start), `POST .../events` (forward turn / interrupt / tool_result / compact / stop / external_session_status forward), `GET .../resources/terminals` (snapshot), `GET .../skills`, `POST .../mcp/execute` | — |
| Runner→Server | tunneled REST (callbacks) | `POST .../events` with `external_*` (native transcript mirror, status, usage, subagent_start, todos), `POST .../policies/evaluate`, `GET .../items` + `GET .../agent/contents` (cold-cache history reload), `PATCH .../{id}` (bind runner_id / cost labels), `POST .../mcp` (proxy) | items/labels durable |
| **Server↔Host** | **WS tunnel** `host_tunnel.py` | `POST /v1/hosts/{id}/runners` launches a runner; `on_runner_connect` binds it | — |

**Trace evidence** (corpus, grouped by `session.id`; one conv = many trace_ids because the
SSE/response path is decoupled from the request — confirmed in all summaries):
- **claude-sdk turn** `conv_32db…`: `server→runner POST /v1/sessions ×3` (start), `runner→server
  GET .../items ×4 + .../agent/contents ×4` (cold-cache history reload), `runner→server
  POST .../policies/evaluate ×2`, `runner→harness POST .../events ×3`, WS-tunnel
  `receive ×71 / send ×21`. Captured `policy.evaluate` payloads show input + assistant-output +
  request-context evaluations (server-side enforcement on both paths).
- **claude-native turn** `conv_94e6…`: `runner→server POST .../events ×14` — dominant edge is the
  **native transcript forwarder** posting `external_*` events back (the single-writer mirror),
  plus `claude_native.forward ×4` / `claude_native.inject ×1`. Confirms native input is NOT
  AP-persisted on the way in; it round-trips back as `external_conversation_item`.
- **subagent spawn** `conv_fc47…`: `runner→harness POST .../events ×10`, `POST .../mcp/execute ×3`
  + `POST .../mcp ×3` (Omnigent MCP proxy), `GET .../child_sessions ×1`, `policy.evaluate ×14`.
  Payloads show `sys_session_send`→worker→`sys_read_inbox` (Polly-style delegation through the
  inbox).

#### 5. CUJ behaviors (server's part)

**Request lifecycle (the happy path), `POST /events type=message`:**
```
post_event(S:18150)
 ├ auth + LEVEL_EDIT (_require_access_and_level)            S:18229
 ├ validate type ∈ _ALLOWED_EVENT_TYPES (S:800); parse data S:18245
 ├ INPUT policy (S:18321) — DENY/ASK ⇒ publish deny sentinel, persist sentinel,
 │   response.completed, idle; return {queued:False,denied:True}  S:18347
 ├ resolve runner (_get_runner_client S:5777); if none: managed-launch rendezvous /
 │   host relaunch / wait-for-runner; else persist-failure-turn (native) or 503  S:18889-19076
 ├ _dispatch_session_event_to_runner(S:8735):
 │   • non-native → _forward_event_to_runner: APPEND item (I1, S:8540) → POST runner /events
 │     (S:8697) → publish session.input.consumed w/ persisted id (S:8704)
 │   • claude-native message → BYPASS persist; record pending_inputs optimistic id;
 │     _forward_native_terminal_message (tmux inject); forwarder mirrors back later  S:8817-8914
 └ return {queued:True, item_id|pending_id}
relay (_relay_runner_stream S:9371): runner SSE → persist durable items
   (_flush_relay_text at tool boundaries, _extract_persistent_item_from_sse) → publish to
   local session_stream → client SSE renders.
```

**Streaming vs durable, incl. reasoning:** SSE `response.output_text.delta` /
`response.reasoning_text.delta` / `reasoning.started` are **transient** (SSE-only); the relay
accumulates text deltas (`S:9544`) and flushes them as a durable `message` item at each
text→tool boundary and on terminal events (`_flush_relay_text S:9270`). **Reasoning deltas are
not persisted as items**; the durable counterpart is the assistant `message` text (reasoning is
streamed for live view only). Tool calls/outputs persist via `output_item.done` (status
`completed` only, `S:8987`). `external_output_reasoning_delta` (`S:18636`) is explicitly transient.

**Dedup (server side):** the server does NOT dedup at the DB (no unique item-id constraint). It
emits the anchors clients dedup against:
- `session.input.consumed` (`S:2537`) carries the persisted `item_id` (+ `cleared_pending_id` to
  drop a native optimistic bubble) → client replaces its temp/optimistic bubble by id.
- `_flush_relay_text` republishes flushed text as `output_item.done` *with the store-assigned id*
  so the client stamps it on the already-rendered streamed block (web by content at boundaries /
  TUI by byte-equal segment, `S:9287-9301`).
- **Runner** dedups on cold-cache reload via `persisted_item_id` (`S:8618`): it drops the
  pre-resolution copy by id and appends its own resolved copy.

**Working vs idle:** computed solely from `_session_status_cache` (`S:12701`: running/waiting ⇒
busy). Every write funnels through `_publish_status` (`S:5343`) which (a) writes the cache and (b)
publishes a typed `session.status` SSE. `failed` is sticky against a trailing `idle` (`S:5389`);
cleared on next `running`. Native harnesses: the forwarder POSTs `external_session_status`
(`S:18667`) → `_publish_status` + forward to runner; PTY-activity idle carries no bg count. Snapshot
projects `busy` + `last_task_error` from cache+labels (`S:12700`).

**Disconnect→reconnect / close-page-and-return:** SSE is live-tail only (no buffer, `S:11240`).
Reconnect contract = `GET /sessions/{id}` snapshot (durable items + status from cache + last_task_error
labels) **then** open a fresh `/stream`; events in the gap are deduped client-side by item id.
`/stream` emits a ready `session.heartbeat` at slot registration (`S:11314`), interval heartbeats
(`S:11313`), `[DONE]` on every exit (`S:11341`), and a snapshot-on-connect of terminals/child-sessions/
presence (`S:19246`). The Databricks Apps ingress ~5-min HTTP/2 cap is handled by transparent client
reconnect, NOT defeated server-side (`S:19348`). Closing the page just drops the SSE/WS; returning
re-runs snapshot+stream. ⚠️ `_session_status_cache`/read-state are in-memory: a server restart mid-turn
loses live status (rederived once the runner re-emits) and resets read/unread.

**Fork (`POST /sessions/{source}/fork`, `S:15189`):** deep-copies items (optionally
`up_to_response_id`, `S:15379`), clones the source/target agent into a **new session-scoped agent**
inside the store transaction (`S:15279`), grants OWNER, `_announce_session_added`. Model settings carry
over only within the same provider family (`_same_provider_family S:15319`). Native targets get a
`FORK_CARRY_HISTORY` label so the runner rebuilds its native transcript from the copied items
(cross-family always rebuilds; same-family may clone the rollout, `S:15323-15352`). ⚠️ Fork is a
**top-level deep copy**, NOT a parent/root pointer — lineage is only the `omnigent.fork.source_id`
label (corrects a likely CUJ-ANALYSIS assumption). Runner is bound afterward by `PATCH .../{id}`.
Fork conv `conv_151ad…` has **0 spans** in the corpus (no turn was run on it post-fork).

**Switch-agent (`POST .../switch-agent`, `S:15423`):** in-place, **idle-only (409 if running,
`S:15481`)**; deletes+re-clones the agent, clears `external_session_id`, recomputes presentation labels,
publishes `session.agent_changed` (`S:15604`), and resets runner-side resources after responding
(`S:14614`). Same session/transcript/host/files. ⚠️ no-op switch to same bundle rejected (`S:15510`).

**Archive / delete:** archive = owner-only `PATCH .../{id} {archived}` (`S:14831`), a Boolean column;
prunes read-state. Delete (`S:19363`) owner-only; best-effort runner-side resource cleanup (404-tolerant),
optional `?delete_branch=true` worktree removal, then deletes the row (CASCADE drops items/labels/perms).

**Resume (new runner) / runner-binding:** `PATCH .../{id} {runner_id}` (`S:14800`) is the affinity
primitive — create-bind/resume-bind/recover-bind all send the live runner id; **owner-only**,
**last-write-wins** (`S:14809`), `""` clears. On resume the server reloads NO transcript itself; the
re-bound runner pulls history on demand via `GET .../items` (paginated, `order=asc`, `limit≤1000`,
`S:16584`) + `GET .../agent/contents` (the ×4–×6 edges in every trace). For native, the CLI's own session
file is authoritative and the runner replays from `external_session_id`/copied items.

**Interrupt fencing:** `type=interrupt` (`S:18439`) publishes `session.interrupted`, adds the session to
`_interrupt_fenced_sessions`, forwards `{type:interrupt}` to the runner (5s); if delivery fails the fence
is lifted so remaining output isn't dropped (`S:18462`). While fenced, the relay drops the turn's trailing
`response.*` (no persist/forward) except elicitation lifecycle (`_FENCE_EXEMPT`, `S:944`); the fence lifts
on the next `running` or any terminal `response.*` (`S:9464`). `stop_session` (`S:18467`) is the same
fence + harness-agnostic runner kill (raises on failure, owner-only) + stops a host-spawned runner; it is
non-sticky so the next message relaunches.

⚠️ **Failure branches:** runner unreachable + non-native item ⇒ 503 (must not persist what the harness
won't see, `S:19073`); runner unreachable + **native** message ⇒ persist a failure turn so it survives
reload (`S:19040`); policy-eval crash ⇒ fail-closed DENY (`S:18331`); relay transport lost ⇒
`session.status=failed{runner_disconnected}` (`S:9823`); compact returns non-204/200 from runner ⇒ error,
no AP-side fallback (`S:18591`).

#### 6. Answers to the doc questions (server scope)

- **Persist-before-forward (I1):** `_forward_event_to_runner` appends (`S:8540`) before POSTing the
  runner (`S:8697`); `input.consumed` only after the forward returns (`S:8704`). The native bypass is the
  one exception (single-writer: forwarder is sole history writer, `S:8767`).
- **Streaming vs durable surface (`ServerStreamEvent`, `schemas.py:3724`):** the union is explicitly
  partitioned — **only `OutputItemDoneEvent` is "POST + SSE replay" (durable)**; everything else is
  SSE-only: `session.*` lifecycle (`status/usage/model/reasoning_effort/agent_changed/todos/
  terminal_pending/sandbox_status/skills/input_consumed/interrupted/created/superseded/presence`),
  resource lifecycle, token deltas (`output_text.delta`, `reasoning.*`), Responses-API turn lifecycle
  (`created/queued/in_progress/completed/failed/cancelled/incomplete`), `compaction.*`, heartbeats,
  elicitation request/resolved.
- **Dedup:** server has **no DB-level dedup**; it provides item-id anchors (`input.consumed`, flushed-text
  `output_item.done`) and the runner uses `persisted_item_id` for cold-cache id-based dedup (§5).
- **Working/idle:** `_session_status_cache` + `_publish_status` (§5); sticky-failed; native via
  `external_session_status`.
- **API surface (REST vs SSE vs WS):**
  - **REST** (55 paths, `<SP>/openapi.json`): session CRUD (`/v1/sessions[...]`), `/events`, `/items`,
    fork, `/switch-agent`, `/agent[...]`, `/agent/mcp-servers`, `/policies` + `/policies/evaluate`,
    `/comments`, `/resources/{terminals,files,environments,shell,search}`, `/permissions`, `/read-state`,
    `/child_sessions`, codex_goal, hosts/runners, `/health`, `/api/version`, `/v1/me`.
  - **SSE**: `GET /v1/sessions/{id}/stream` — the `ServerStreamEvent` union above.
  - **WS**: `/v1/sessions/updates` (sidebar), `/v1/runners/{id}/tunnel`, host tunnel, terminal-attach.
- **Sidebar fetching:** `WS /sessions/updates` replaces the old 4s poll: client sends `{watch,
  session_ids}`; server snapshots then diffs watched rows each `_SESSION_UPDATES_RESCAN_INTERVAL_S`
  (`S:14692`); *discovery* of new/forked/shared sessions is push-based via `user_session_stream`
  → `changed` (`S:14717`). Idle list = zero HTTP. `GET /v1/sessions` (`S:14241`) deliberately omits
  per-row liveness (`S:14389`).
- **Inbox / subagents:** server mints child sessions from `external_subagent_start` (`S:18827`) and
  `sys_session_send`; sub-agent terminal status forwards to the parent's runner inbox, healing a stale
  child `runner_id` via the parent's live runner (`_recover_subagent_status_forward_via_parent`, `S:18752`).
- **Policy enforcement (server-level):** evaluated in `post_event` BEFORE persist/forward on both input
  and output paths (`S:18296`); runner-side tool gates call back via `POST .../policies/evaluate`
  (corpus: `policy.evaluate` spans). Fail-closed on evaluator error (`S:18331`).
- **Smart routing / model & effort:** first-message model routing in `_forward_event_to_runner`
  (`S:8653`, persists `model_override`); mid-session model/effort via `PATCH .../{id}`
  (`S:14917/14900`) or terminal-observed `external_model_change`/`external_reasoning_effort_change`
  (`S:18800/18808`). Per-event `model_override` wins, else the persisted column (`S:8631`).

#### 7. Reliability gaps / sharp edges (confirmed in code)

1. **All live status/presence/read/fence state is in-memory & single-replica** (`S:837,857,869,931`).
   Multi-replica AP: a stream on replica B never sees a status published on replica A (the cache and
   `session_stream` are process-local); a server restart mid-turn shows stale/lost "working" until the
   runner re-emits, and silently resets all read/unread. The code documents status as "rederivable from
   the runner" (`S:863`) but read-state and the interrupt fence have **no durable source**.
2. **Interrupt fence leak window:** if interrupt/stop delivery fails the fence is lifted (`S:18465`), but if
   the runner dies *after* fencing without emitting a terminal `response.*` or `running`, the fence is only
   cleared in the relay `finally` via discard — a fence set by replica A is invisible to replica B.
3. **Background-task tally only refreshes at a turn boundary** (`S:847`): a shell that exits while idle can
   show "N background tasks running" in chat/sidebar/reload until the next message.
4. **`failed`→sticky relies on the assumption no flow does a legit `failed`→`idle`** (`S:5389`); a new
   harness that does would have its error erased.
5. **Forward-after-persist soft failure:** if the runner POST fails after the item is persisted,
   `_forward_event_to_runner` logs, publishes `idle`, and returns the id (`S:8705`) — the user message is in
   the store but the turn never started and no error item is written (silent no-op until the next message).
6. **No DB dedup on items** (`db_models.py:497`): correctness depends entirely on client/runner id-based
   dedup discipline; a buggy forwarder that re-posts an `external_conversation_item` would double-persist.
7. **Snapshot/stream race is client-reconciled, not server-ordered:** events between snapshot read and
   stream subscribe are only deduped if the client honors item-id; the server gives no sequence/cursor on
   the SSE stream (live-tail has no replay/`Last-Event-ID`).

#### 8. Corrections to CUJ-ANALYSIS

(Line numbers in CUJ-ANALYSIS.md drift; these are claim-level corrections from confirmed code.)

1. **§2.A request lifecycle — the "create-or-steer task" / tasks-table model is stale.** There is **no
   `tasks` table** (dropped, migration `b9c1d2e3f4a5`; comments at `S:12700`, `S:14343`). A turn is started by
   `POST runner /v1/sessions/{id}/events` returning 202; the runner runs it as a background task and streams
   via `GET /stream`. "Busy" comes only from the relay-fed `_session_status_cache` (`S:12701`), not a task row.
   Any anchor describing an in-process task object / `_create_and_start_task` / `_steer_active_task` is gone —
   the steer-vs-create branch is now entirely runner-side (the server forwards harness-agnostically).

2. **§2.E (status/dedup) — "server dedups by itemId / `session.input.consumed` consumed-set" is
   imprecise.** The server enforces **no** item-id uniqueness (only `(conversation_id, position)`,
   `db_models.py:497`). `session.input.consumed` (`S:2537`) is a *client* dedup anchor (carries the persisted
   id so the client swaps its optimistic bubble); cold-cache dedup is the **runner's**, keyed on
   `persisted_item_id` (`S:8618`). If the analysis claims a server-side "consumed item id set", that does not
   exist.

3. **§5 (fork/resume) — two corrections.** (a) **Fork is a top-level deep copy, not a parent/root chain**:
   `parent_conversation_id`/`root_conversation_id` are for sub-agents; fork lineage is only the
   `omnigent.fork.source_id` label and copied items get fresh ids (sub-agent map of
   `omnigent/stores/conversation_store/__init__.py:23`, `fork_conversation` at `S:15366`). (b) **The server
   loads no transcript into the runner on resume**; the re-bound runner pulls history via `GET .../items` +
   `GET .../agent/contents` (the ×4–×6 edges in every corpus trace), and native targets replay from their own
   session file / copied items driven by the `FORK_CARRY_HISTORY` label (`S:15338`). Resume-bind itself is just
   a **last-write-wins** `PATCH .../{id} {runner_id}` (`S:14809`), not a CAS.

---

### Component: RUNNER

The per-conversation worker process that hosts the harness/executor, owns MCP + tool
execution + system resources (shells/cwd/timers), and reaches the server **only** over an
outbound WebSocket reverse-tunnel. All `path:line` anchors below were opened and confirmed in
`/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`.

---

#### 1. Role & boundaries

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

#### 2. Key files & entrypoints (verified)

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

#### 3. Internal model

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

#### 4. Inter-component channels

##### 4a. The ONLY production server↔runner transport: the WS reverse-tunnel
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

##### 4b. Runner → server callbacks (HTTP, riding the SAME tunnel back; auth = `_RunnerDatabricksAuth`)
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

##### 4c. Server → runner (HTTP over tunnel) — corpus `omni-server → omni-runner`
`POST /v1/sessions` (create/start), `GET /v1/sessions/{id}` (probe), `POST …/events` (turn drive + control events), `POST …/mcp/execute` (tool exec), `GET …/resources/terminals`, `GET …/skills`. All dispatched via `RunnerRouter.client_for_session_resources`/`client_for_conversation` (routing.py:89/118) → `WSTunnelTransport`.

##### 4d. Runner → harness (real HTTP over a UDS; runner is always the client)
The live path is **not** in-process: the harness is a child subprocess (`python -m omnigent.runtime.harnesses._runner`, one per (conversation, harness, model)) running a `uvicorn.Server` over a per-harness `create_app()` FastAPI app bound to a Unix domain socket; the runner reaches it with `httpx.AsyncHTTPTransport(uds=…)` (`process_manager.py:1027,1054`). `POST /v1/sessions/{conv}/events` — corpus edge `omni-runner → omni-harness` ×10 (subagent). The direction is one-sided: **the runner is always the HTTP client; the harness only responds and never calls the runner.** The turn is ONE long-lived streaming request (`client.stream(...)`, `app.py:14215`); ExecutorEvents come back as SSE frames on that request's response body, consumed by `_stream_message_to_harness` (`app.py:14892`). When the harness needs a tool run or policy verdict it emits `action_required` / `policy_evaluation.requested` in that stream; the runner services it and posts the result back with a **separate short** `POST …/events` (`{tool_result,call_id}` `tool_dispatch.py:4346`; `{policy_verdict}` `_evaluate_policy_via_omnigent` `app.py:6261`) — a POST, not a PATCH. For **native** harnesses the executor's `run_turn` does ONE inject (tmux send-keys) and returns an empty stream; the transcript is mirrored back out-of-band by the runner-resident forwarder task (see §7-Executor). Interrupt routing app.py:14928-14958.

---

#### 5. CUJ behaviors (runner's slice)

##### Request lifecycle (forwarded input → executor → output back)
`POST /v1/sessions/{conv}/events` (app.py:14598):
1. **FIFO ingest gate** (app.py:14692-14700): read-increment `_ingest_next_seq` synchronously *before any await*, then wait on `_ingest_cond` until `_ingest_now_serving == my_seq`. Guarantees arrival-order across content-resolution latency (the "first-message FIFO desync" fix, PR #1457). `finally` advances the gate even on error (app.py:14924).
2. **Content resolution** (`_resolve_forwarded_message_content`, app.py:14704) — resolves `file_id`→`image_url`/`file_data` blocks.
3. **Buffer-vs-turn decision** (app.py:14711): if `conv in _active_turns`, buffer (`_session_message_buffers`). Forward as a **live injection** only if non-native **and** not awaiting-approval **and** `conv in _live_response_id` (app.py:14741). Native: never forward (instant turns race teardown) — always drained via post-turn continuation (app.py:14766). Awaiting-approval: buffer-only, no forward (can't steer a human-gated turn, app.py:14718-14726).
4. **Start turn** (app.py:14860): `_active_turns[conv]=None`, `_publish_turn_status(conv,"running")`. Cold cache → rehydrate via `_load_history_as_input` (drop just-persisted pre-resolution item, append resolved, app.py:14852-14858).
5. **Output back, two modes:**
   - `stream=true` → `StreamingResponse` whose **body IS the SSE** (app.py:14892); harness `response.created`→tool-dispatch→pairing flow consumed inline.
   - `stream=false` → background `_run_turn_bg` task, **202 Accepted**; events flow out via `GET /v1/sessions/{conv}/stream` (app.py:14903). Corpus shows both `GET …/stream` ×2 and the 202 path.

##### Disconnect / reconnect (runner side)
- **Runner is WS client**; on any drop `serve_tunnel` retries forever: 0.5s→10s cap, ±50% jitter (serve.py:74-76). Backoff escalates only on non-recycle failures.
- **Routine ingress recycles** (close 1001/1012, HTTP 502 — Databricks Apps cycling long-lived WS) reset backoff to base, no escalation (serve.py:87,343-353) — keeps the runner registered.
- **Fatal:** close 4001/4002/4004/4500 or HTTP 403 → `RuntimeError` (give up). 401 → refresh token once and retry (serve.py:326). InvalidURI redirect to http(s) (Apps OAuth login) → fail loud with "run `omnigent setup`" (serve.py:309-324).
- **On reconnect** (`on_reconnect=catch_up_scan`, app.py:18399): for each session with in-memory history, paginate `GET …/items` after `_last_server_item_id`, append new items, and **auto-start a turn** if idle + last new item is a `user` message (app.py:18449). ⚠️ **Native harnesses are skipped** (app.py:18408) — they don't replay mirrored transcript items.
- **Server side on tunnel close** (`registry.deregister`, registry.py:299): aborts all in-flight requests with `ConnectionError` so awaiters fail fast rather than hang; fires `on_runner_disconnect` (server marks sessions `runner_offline`).

##### Resume (how much transcript loads into the runner?)
**The full transcript.** `_load_history_as_input` (app.py:9834) paginates `GET /v1/sessions/{id}/items?limit=100&order=asc&after=…` until `has_more=false`, converts every item to Responses-input shape, caches in `_session_histories`. There is no windowing here; compaction is applied upstream by the server's item store, not the runner.

##### Fork / new-runner resume
A resumed session (possibly on a **new** runner after relaunch) gets a fresh `runner_id`; the server rebinds via `PATCH`/host-launch (`replace_runner_id`). The runner rehydrates history lazily on first turn. ⚠️ Native **sub-agent** children copy the parent's `runner_id` once at creation (sessions.py:5195-5196) and can point at a dead runner after relaunch — healed via parent on forward (sessions.py:5209-5253, PR #1446).

##### Policy enforcement (two distinct paths — important)
- **SDK/proxy path (claude-sdk, codex):** runner enforces `function` TOOL_CALL/TOOL_RESULT policies **locally** via `RunnerToolPolicyGate` (policy.py). DENY → refusal text fed back as tool output (loud-fail). ASK → runner escalates by `POST …/policies/evaluate` to the server, which parks an elicitation; runner awaits via `pending_approvals` (policy.py:20-28). `__agent_start` reuses the same gate (app.py:18774-18800). `label`/`prompt` policies always stay server-side.
- **Server-routed MCP path:** server's `POST /v1/sessions/{id}/mcp` evaluates TOOL_CALL/TOOL_RESULT centrally, **then** forwards `tools/call`/`tools/list` to runner's `POST …/mcp/execute` (app.py:17829) over the tunnel. The runner-side `/mcp/execute` does NOT re-run policy — execution only (tool_dispatch.py:4022-4025 comment: "routed through the AP server's /mcp endpoint … No runner-side policy gate needed"). Corpus `conv_fc47380…` shows `server→runner POST …/mcp/execute ×3` + `runner→server POST …/mcp ×3`.

##### MCP routing (custom + Omnigent MCP)
- Custom MCP servers: `RunnerMcpManager` spawns/holds stdio subprocess connections, prewarmed per spec_hash, namespaced `server__tool` (mcp_manager.py).
- Omnigent system tools (`sys_os_*`, `sys_terminal_*`, `sys_session_*`, etc.): runner-local, dispatched by `tool_dispatch.execute_tool` (categories at app.py via `_OS_ENV_TOOLS`/`_TERMINAL_TOOLS`/…).
- Inline MCP elicitation: runner POSTs `{type:"mcp_elicitation"}` to `…/events`, parks on `pending_approvals`; on no server client, declines (mcp_manager.py:182-277).

##### System resources (shells, cwd, timers)
Runner-owned via `resource_registry.py`: terminals are tmux panes launched by the runner (`sys_terminal_launch`), shared cwd from `RUNNER_WORKSPACE`/session-isolation (`OMNIGENT_RUNNER_ISOLATE_SESSION`), timers in `_session_timers`. Browser attach proxies the pane over the tunnel WS channel (4a).

##### Close page & idle
Idle watchdog (`_run_inactivity_monitor`, default 1h `_DEFAULT_RUNNER_IDLE_TIMEOUT_S`) shuts the runner down when no real activity AND no active work (`_entry.py:909,935-943,965`). Tmux-detach "adopt" (SIGUSR1) makes the parent-death killer stand down so the runner outlives the CLI (identity.py:14-19).

---

#### 6. Answers to the doc questions (runner area)

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

#### 7. Reliability gaps / sharp edges (confirmable in code)

1. **Hard affinity + no failover** (routing.py:107-116): if the bound runner is offline and never reconnects (e.g. host died, no relaunch), every dispatch raises `RUNNER_UNAVAILABLE`. Recovery depends entirely on the host relaunching the runner under the *same* `runner_id` (token-bound) or the server rebinding. No automatic migration of a live conversation to another runner.
2. **Native sub-agent stale `runner_id`** (sessions.py:5195-5253): child copies parent's `runner_id` at creation; after a parent-runner relaunch the child points at a dead runner. Mitigated by heal-via-parent on forward, but the child is broken until a forward triggers the heal.
3. **Catch-up auto-turn only for non-native** (app.py:18408,18449): a native session that received a user message during a disconnect won't get a synthesized catch-up turn; delivery depends on the native pane's own state. Non-native sessions auto-start a turn purely from "last item is user" — a benign duplicate user item could in principle start an unwanted turn.
4. **Rebind path (`replace_runner_id`) is LWW with no guard** (sqlalchemy_store.py:1973), unlike the initial pin which is a proper CAS (`set_runner_id` :1918). So two concurrent *rebinders* (e.g. `_on_runner_connect` + message-path relaunch handshake — the race the `_claude_terminal_ensure_locks` comment at app.py:7877-7884 calls out) can stomp each other's `runner_id`. The per-session ensure-locks guard terminal double-launch but not the DB rebind itself.
5. **In-flight requests die on tunnel drop** (registry.py:334-337): mid-request server→runner calls get `ConnectionError`. For a streaming turn this surfaces as an aborted SSE; idempotency/retry is the caller's problem.
6. **`HelloFrame.harnesses` is a hardcoded list** (serve.py:581-592): `claude-native, claude-sdk, codex, openai-agents, open-responses, pi`. ⚠️ It does **not** include `codex-native` (nor several others the runner actually serves: hermes/cursor/goose/kiro/qwen/kimi native, per app.py interrupt handlers). `_runner_supports_harness` (routing.py:269) gates dispatch on this list, so a conversation whose spec resolves to a harness absent from the hello frame would be rejected with `RUNNER_CAPABILITY_MISMATCH` even though the runner can run it. (Flagged for the stitcher — confirm whether `codex-native`/other native kinds canonicalize to one of the listed keys; `_EXECUTOR_TYPE_TO_HARNESS` only maps `claude_sdk`.)
7. **Idle watchdog vs adopted runner** (_entry.py:909): idle timeout still fires for a detached/adopted runner serving only the web UI if it sees no "activity" frames for an hour, even though a user might return — the 1h budget is the only knob.

---

#### 8. Corrections to CUJ-ANALYSIS

Per the briefing, I treated CUJ-ANALYSIS §2.F/§2.G as hypotheses. I could not open `designs/CUJ-ANALYSIS.md` in this pass (not located under the worktree root via the read tools I used), so these are **corrections framed against the briefing's stated claims** for §2.F (runner dispatch) / §2.G (runner↔server auth) — flag any that the actual doc already states correctly:

1. **"binding CAS" applies only to the INITIAL pin, not rebind (§2.F).** There are two distinct store methods: `set_runner_id` is a real CAS (`UPDATE … WHERE runner_id IS NULL`, sqlalchemy_store.py:1918-1949) used for new conversations; `replace_runner_id` is unconditional LWW (sqlalchemy_store.py:1951-1975) used for resume / host re-launch / subagent heal. Any claim that binding is *always* CAS, or *always* LWW, is half-wrong. Separately, the **tunnel-session newest-wins** in `TunnelRegistry.register` (registry.py:280-297) is a different object (live WS session, not the DB row) — do not conflate it with either DB method.
2. **Auth refresh is two different cadences, not one (§2.G).** Tunnel (WS) Bearer refreshes **per reconnect** (`serve.py:284` + 401 retry); HTTP callbacks refresh **per request** with a 401-**or**-302-to-`/oidc/` retry (`_RunnerDatabricksAuth.auth_flow`, _entry.py:192-238). Any claim that "the tunnel refreshes tokens per message" or that "401 is the only re-auth trigger" is wrong — the Apps front door 302s instead of 401, which is why the redirect branch exists.
3. **`routing.py`/`transport.py` run in the SERVER, not the runner (§2.F).** Despite living under `omnigent/runner/`, `RunnerRouter` + `WSTunnelTransport` execute in the server process (they import the server's `ConversationStore`/`TunnelRegistry`). The runner-side tunnel code is `transports/ws_tunnel/serve.py`. Any anchor attributing dispatch/affinity logic to "the runner process" is mislocated.

(Trace evidence: `conv_fc47380ccbff481abf452a446ec4e40d` and `conv_32db3f5927d9459fa028cbe69d4173d3` summaries — every runner↔server edge enumerated in §4b/§4c was matched to a handler above.)

---

### Host Daemon (`omnigent host` / service `omni-host`)

> All `path:line` anchors below were opened and confirmed against the worktree
> `master-arch-docs` (main + telemetry PR #1617). Items I could not confirm in code are tagged `(unverified)`.

#### 1. Role & boundaries

The **host daemon** is a long-lived process (`omnigent host`) that runs on a *machine* (a
user laptop, or a server-provisioned managed sandbox) and lets the **server launch and
manage runner subprocesses on that machine on the server's behalf**. It is the server's
remote hand on a host it cannot reach directly: the host dials *out* to the server over one
WebSocket, and the server pushes control frames down it.

What it **owns**:
- One outbound WS "host tunnel" to the server (`omnigent/host/connect.py`).
- Spawning runner subprocesses (`python -m omnigent.runner._entry`) and watching them
  (`HostProcess._runners`, `connect.py:560`, `_handle_launch` `connect.py:744`).
- The **host→runner spawn environment** — a strict allowlist (`_RUNNER_ENV_ALLOWLIST`,
  `connect.py:203`) so the host owner's shell secrets don't leak into runners.
- Host-local **filesystem queries** the server needs before any runner exists: `stat`,
  `list_dir`, `create_dir`, and git `create_worktree` / `remove_worktree`. The host is the
  source of truth for `~` expansion — the server never expands tildes (`frames.py:206-216`).
- Host **identity** (`host_id` + name) in `~/.omnigent/config.yaml` (`identity.py:54`).

What it does **NOT** own / is NOT:
- It carries **no HTTP request/response traffic** for sessions. Runners connect to the
  server with their *own* WS tunnels; the host tunnel is control-only (`frames.py:8-11`,
  `host_registry.py:9-12`).
- It does NOT hold session/conversation state, transcripts, MCP routing, policy, or the
  brain. Once a runner is spawned it talks to the server independently — the host never sees
  the turn.
- It is NOT a sandbox itself; on managed deploys the *provider* (Modal/Daytona/k8s/…)
  provisions the box and the server execs `omnigent host` inside it.

#### 2. Key files & entrypoints (verified)

| Path:line | What |
|---|---|
| `omnigent/host/frames.py:37` | `HostFrameKind` enum — all 16 wire-string kinds |
| `omnigent/host/frames.py:524` / `:700` | `encode_host_frame` / `decode_host_frame` |
| `omnigent/host/frames.py:500-521` | `_encode_payload` injects W3C `traceparent` into every frame (telemetry PR) |
| `omnigent/host/connect.py:537` | `HostProcess` — daemon lifecycle |
| `omnigent/host/connect.py:744` | `_handle_launch` — spawn a runner (harness gate, workspace check) |
| `omnigent/host/connect.py:410` | `_build_runner_env` — host→runner env allowlist |
| `omnigent/host/connect.py:1409` | `_build_connect_headers` — tunnel auth (managed token vs Bearer) |
| `omnigent/host/connect.py:1462` | `_serve_frames` — hello + receive loop |
| `omnigent/host/connect.py:1513` / `:1556` | `_handle_raw_message` / `_dispatch_host_frame` — frame routing (+ `consume_frame_span`) |
| `omnigent/host/connect.py:1269` | `run()` — reconnect loop w/ recycle classification |
| `omnigent/host/identity.py:25-38` | `HOST_TOKEN/ID/NAME` env vars + `MANAGED_HOST_TOKEN_HEADER` |
| `omnigent/server/routes/host_tunnel.py:120` | server WS endpoint `/v1/hosts/{host_id}/tunnel` |
| `omnigent/server/routes/host_tunnel.py:369` | `_receive_loop` — resolves result frames to pending futures |
| `omnigent/server/host_registry.py:136` / `:220` | `HostConnection` / `HostRegistry` (in-memory, per-replica) |
| `omnigent/server/routes/hosts.py:405` | `POST /v1/hosts/{host_id}/runners` (launch route) |
| `omnigent/server/routes/sessions.py:6035` | `_launch_runner_on_host` (relaunch path on resume/new-runner) |
| `omnigent/server/managed_hosts.py:1690` / `:1842` | `launch_managed_host` / `_arm_and_start_host` |
| `omnigent/onboarding/sandboxes/base.py:152` | `SandboxLauncher` ABC (`prepare`/`provision`/`start_host`) |
| `omnigent/onboarding/sandboxes/base.py:319-326` | injects `OMNIGENT_HOST_TOKEN/ID/NAME` + execs `omnigent host` in box |
| `omnigent/stores/host_store.py:611` | `resolve_launch_token` (SHA-256 digest auth for managed hosts) |
| `omnigent/cli.py:6645` | `omni host` CLI group |
| `omnigent/cli.py:2267` | `_build_host_daemon_env` (background-daemon env allowlist) |

#### 3. Internal model

**Host side** (`HostProcess`, `connect.py:537`):
- `_runners: dict[runner_id -> _RunnerHandle]` — each handle is `(subprocess.Popen, log_path)`
  (`connect.py:522`). Runner stdout/stderr is captured to `~/.omnigent/logs/host-runner/runner-*.log`.
- `_watcher_tasks` — one `_watch_runner` task per spawned runner (`connect.py:898`); polls every
  0.5s; an exit *while still in `_runners`* is unexpected (a stop pops the entry first) → composes
  an exit error (code + log tail) and sends `host.runner_exited`.
- `_unreported_exits: dict[runner_id -> error]` — exits that raced a dead tunnel; flushed right
  after the next `host.hello` (`connect.py:1491`).
- `_ever_connected`, `_login_redirect_streak` — drive the fail-loud-vs-retry decision on auth
  redirects (`connect.py:563-567`).

**Server side** (per replica, `host_registry.py`):
- `HostRegistry._hosts: dict[host_id -> HostConnection]` — newest-wins; replacing a stale conn
  poisons its outbound queue with `None` (`host_registry.py:264-271`).
- `HostConnection` (`:136`) holds: `ws`, the `hello` frame, `owner`, an `outbound_queue`
  (drained by `_sender_loop`), `last_frame_at`, and **seven `pending_*` dicts** of
  `request_id -> asyncio.Future` (launches/stops/stats/list_dirs/create_worktrees/
  remove_worktrees/create_dirs). Each request creates a future; `_receive_loop` resolves it
  when the matching `*_result` frame arrives. This is how a synchronous-feeling REST call is
  multiplexed over the async control tunnel.
- `RunnerExitReports` (`:55`) — TTL cache (600s, 1024 entries) of `host.runner_exited` causes,
  **owner-scoped** so the runner-status endpoint can answer "offline, and here's why" without
  leaking another user's log tail.
- **Persistent** cross-replica truth is the `hosts` DB table via `HostStore` (registry is only
  "live *here*"). Liveness = `status=="online" AND updated_at >= now - 90s`
  (`host_store.py:35,98` `HOST_LIVENESS_TTL_S`); the ping loop heartbeats every 30s so a host
  that dies without a graceful close drops out of the connected set when the timestamp goes stale.

#### 4. Inter-component channels

The host has exactly **one** transport edge: a single **WS control-frame tunnel** to the
server. Everything else (the runner's session traffic) is on *other* tunnels the host never
touches.

```
                    ┌──────────────────────── Server (omni-server) ────────────────────────┐
  ┌──────────┐  WS  │  /v1/hosts/{id}/tunnel  ── HostRegistry / HostConnection.pending_*   │
  │  Client  │ REST │      ▲ host.hello (host→srv, on connect)                              │
  │ TUI/Web  │─────▶│  POST /v1/hosts/{id}/runners ─┐  GET /v1/hosts/{id}/filesystem/...    │
  └──────────┘      │  POST /v1/sessions (managed)  │  POST /v1/hosts/{id}/directories      │
                    └───────────────────────────────┼──────────────────────────────────────┘
                                                     │  host.* control frames (JSON, WS text)
                              ┌──────────────────────▼─────────────────────┐
                              │        Host Daemon (omni-host)              │
                              │  _handle_launch → subprocess.Popen          │
                              └──────────────────────┬─────────────────────┘
                              env-based, ONE-WAY     │  spawn: python -m omnigent.runner._entry
                              (no fd/pipe back)       ▼
                              ┌─────────────────────────────────────────────┐
                              │  Runner (omni-runner) ── its OWN WS tunnel ──┼──▶ server (separate edge)
                              └─────────────────────────────────────────────┘
```

**Channel: Host ↔ Server — WS JSON control-frame tunnel** (NOT HTTP).
- URL `wss://<server>/v1/hosts/{host_id}/tunnel` (`connect.py:590`). Host is the WS *client*
  (dials out); server is the endpoint (`host_tunnel.py:120`).
- **Frame envelope**: every message is one WS *text* frame = a JSON object with a `kind`
  discriminator (`HostFrameKind` value) + typed fields. Encode/decode in `frames.py`. The
  telemetry PR added a centralized `_encode_payload` (`frames.py:500`) that, inside an active
  span, injects W3C `traceparent`/`tracestate` keys into the JSON (decoders ignore unknown
  keys → wire-compatible). That is how the otherwise-invisible host↔server boundary joins the
  distributed trace; the receiver re-parents via `consume_frame_span` (`connect.py:1553`).
- The tunnel **multiplexes two frame families** on one socket: `host.*` frames *and* the
  runner-tunnel `ping`/`pong` keepalive (`PingFrame`/`PongFrame`, reused from
  `runner.transports.ws_tunnel.frames`). A frame that fails `decode_host_frame` is retried as
  `decode_frame`; an unknown frame is *ignored* (forward-compatible) (`connect.py:1527-1538`,
  `host_tunnel.py:399-431`).

**The 16 HostFrameKinds** (server→host request paired with host→server `*_result`, except the
one-way `hello`/`runner_exited`):

| Kind | Dir | Carries / does |
|---|---|---|
| `host.hello` | host→srv | first frame: `version`, `frame_protocol_version` (strict-major; server refuses mismatch w/ close 4002, `host_tunnel.py:214`), `name`, **`runners`** (live runner ids → reconnect reconciliation), **`configured_harnesses`** (per-harness readiness map; `None`=unknown, never "nothing configured") (`frames.py:61-86`) |
| `host.launch_runner` (+`_result`) | srv→host | `request_id`, **`binding_token`** (server derives `runner_id` via `token_bound_runner_id`), `workspace`, `harness`. Host gate: refuse if harness not configured (`error_code=harness_not_configured`) or workspace not a dir, else `Popen` (`frames.py:89-139`, `connect.py:744`) |
| `host.stop_runner` (+`_result`) | srv→host | terminate a runner pid (SIGTERM, 5s, then kill) (`connect.py:863`) |
| `host.runner_exited` | host→srv | **one-way**, no result. Composed cause = exit code + host log path + log tail. Only failure signal for a runner that died *before* connecting its own tunnel (`frames.py:172-194`) |
| `host.stat` (+`_result`) | srv→host | session-create workspace validation; returns `exists`/`type`/`canonical_path` (realpath, defeats symlink escape). ENOENT+EACCES collapse to `exists:false` (`frames.py:196-255`, `connect.py:952`) |
| `host.list_dir` (+`_result`) | srv→host | Web-UI directory picker before any runner exists; paginated by entry-path cursor (`frames.py:289`, `connect.py:1025`) |
| `host.create_dir` (+`_result`) | srv→host | Web-UI "make a new folder" in the picker (`connect.py:1122`) |
| `host.create_worktree` / `host.remove_worktree` (+`_result`) | srv→host | fork-resume git worktree on the host repo; blocking git runs in a worker thread (`connect.py:1194,1233`) |

**Server-internal trigger → tunnel** (NOT a network edge, same process): a REST handler builds
a `HostLaunchRunnerFrame`, registers a future on `conn.pending_launches[request_id]`, and
calls `host_registry.send_text(conn, frame)` which `put_nowait`s onto the `outbound_queue`
that `_sender_loop` drains (`hosts.py:605-625`, `host_registry.py:303`). `_receive_loop`
resolves the future when `host.launch_runner_result` returns (`host_tunnel.py:433`).

**Trace evidence** (live corpus, service `omni-host`): trace `fb469fff…` and `3349da47…` show
exactly:
```
[omni-server] POST /v1/hosts/{host_id}/runners        (16ms)  payload kind=host.launch_runner, workspace=/home/dhru…
  [omni-host]   host.stat            +6.9ms (0.3ms)  payload kind=host.stat_result  exists=true type=directory
  [omni-host]   host.launch_runner   +13.2ms (2.2ms) payload kind=host.launch_runner_result status=launched runner_id=runner_token…
```
i.e. one POST does a `host.stat` (workspace validation) **then** `host.launch_runner`, both
nested under the server span via the injected `traceparent`. (Matches OBSERVABILITY.md §6.4/§10.4.)
Note: these host traces carry `session.id=None` — the launch route is keyed by `host_id`, not
the session, so the host control plane is **decoupled from any one conv's trace group**. The
runner the launch spawns then shows up as `omni-runner` in the session's own traces
(e.g. conv_32db…), which is where the host's local server got the launch workspace from.

#### 5. CUJ behaviors

**Session create on an external host** (laptop running `omni host <url>`):
1. Client → `POST /v1/sessions` (or `POST /v1/hosts/{id}/runners` for the fork-resume picker).
2. Server validates workspace against the agent's `os_env.cwd` boundary via `host.stat`
   (`hosts.py:454-471` calls `validate_workspace`; the stat round-trips the tunnel). Canonical
   realpath is stored, not user input (symlink-escape defense).
3. Server atomically binds `runner_id` (UPDATE … WHERE runner_id IS NULL — closes the launch
   TOCTOU, `hosts.py:580`), persists `host_id`+workspace, then sends `host.launch_runner`.
4. Host spawns the runner; result frame returns `launched`/`failed`. Server does NOT wait for
   the runner to *connect* here — it returns `status:"launching"` and the client polls runner
   status separately (`_launch_runner_on_host` `sessions.py:6048`).
5. Runner dials its *own* tunnel; from here the host is out of the loop for the turn.

**Managed sandbox** (server provisions the box, `host_type="managed"`):
`launch_managed_host` (`managed_hosts.py:1690`): `launcher.prepare()` → `provision(name)` →
`_arm_and_start_host`: `register_managed_host` arms a SHA-256 token digest in the `hosts` row
**before** the box starts (closes the dial-back race), then `launcher.start_host(...)` execs
`OMNIGENT_HOST_TOKEN=… OMNIGENT_HOST_ID=… OMNIGENT_HOST_NAME=… omnigent host --server <url>`
**inside the sandbox** (`base.py:319-326`), detached via `run_background`. Server polls
`hosts.is_online` until the host registers (`MANAGED_HOST_ONLINE_TIMEOUT_S`). Any
post-provision failure tears the box down and revokes the token. The managed identity is
durable while the box is ephemeral: `relaunch_managed_host` (`:1764`) keeps the row, provisions
a fresh box, re-arms a new token (atomically revoking the old). `resume_managed_host` (`:2032`)
wakes a *resumable* provider (`can_resume`, persistent volume) in place instead of reprovisioning.

**Reconnect / disconnect** (`connect.py:1269` run loop):
- Backoff 0.5s→10s with jitter. **Recycle classification**: explicit `1012`/`1001` close, or a
  `no close frame`/`502` on a *remote* server (Databricks Apps ingress cycling a live WS) →
  *prompt* 0.5s reconnect so the tunnel isn't down long enough to drop a `launch_runner`. On a
  **loopback** server an abrupt drop is real (e.g. re-registration) → normal backoff, so fast
  reconnects don't *fuel* a registration flap (`connect.py:1300-1345`). ⚠️ This is a subtle,
  URL-dependent branch.
- On reconnect the host re-sends `hello` with current `runners` + freshly recomputed
  `configured_harnesses`, and flushes `_unreported_exits`. Server `on_host_connect` does
  reconcile (`host_tunnel.py:261`).
- ⚠️ **Auth-redirect fail-loud**: a host that has *never* connected and hits ≥3 consecutive
  login-page redirects raises `HostConnectError` → `omni host` exits 1 with a fix hint. An
  *already-connected* host retries redirects forever (a deploy restart never kills a live host)
  (`connect.py:635-696`, `:1286-1290`).

**Stop / cleanup**: Ctrl-C/SIGTERM → `_cleanup_runners` SIGTERMs all live runners
(`connect.py:1351`). Server side: tunnel close → `deregister` + `host_store.set_offline`
(guarded so a *failed pre-register* connect can't flip another owner's host offline,
`host_tunnel.py:307-330`).

#### 6. Answers to the doc questions (host's area)

- **Host daemon's role**: see §1 — the server's remote launcher/manager of runner subprocesses
  on a machine, plus host-local fs/git/stat queries; control-plane only.
- **What flows host↔server, over what channel**: §4 — a single **WS JSON control-frame
  tunnel** (`/v1/hosts/{id}/tunnel`), NOT HTTP. Envelope = JSON object w/ `kind` + typed fields
  + injected `traceparent`; 16 `HostFrameKind`s; multiplexed with ping/pong keepalive.
- **How runners launch on a host (managed sandbox)**: §5 — token armed in DB → `omnigent host`
  execed in the box with `OMNIGENT_HOST_*` env → host dials tunnel → server sends
  `host.launch_runner` → `subprocess.Popen([python, -m, omnigent.runner._entry])`.
- **Host→runner spawn (env-based, one-way)**: `_build_runner_env` (`connect.py:410`). The runner
  inherits only `_RUNNER_ENV_ALLOWLIST` (`connect.py:203` — PATH/HOME/locale/TLS-trust +
  explicitly-justified knobs like `IS_SANDBOX`, `OMNIGENT_CONFIG_HOME/DATA_DIR`,
  `OMNIGENT_AUTH_*`, `DATABRICKS_CONFIG_PROFILE/FILE`) + prefixes (`LC_/MLFLOW_/OTEL_/OMNIGENT_OTEL_`)
  + **harness credentials** (`HARNESS_CREDENTIAL_ENV_VARS`, `connect.py:352` — `ANTHROPIC_*`,
  `OPENAI_*`, `GEMINI_API_KEY`, `CODEX_ACCESS_TOKEN`, `GIT_TOKEN`, …) + operator extras via
  `OMNIGENT_RUNNER_ENV_PASSTHROUGH`. Then it stamps wiring vars `RUNNER_SERVER_URL`,
  `OMNIGENT_RUNNER_ID`, the binding token, workspace, parent pid (`connect.py:456-460`). The
  channel is **one-way**: env at spawn + a captured-log file; there is **no pipe/fd back** —
  stdin is `/dev/null` (`connect.py:816`), stdout/stderr go to a log file, and the daemon learns
  the runner's fate only by polling `proc.poll()`. Everything else the runner needs flows over
  the runner's *own* server tunnel, not from the host.
- **Host↔server auth** (`_build_connect_headers` `connect.py:1409`; server `host_tunnel.py:134-204`):
  authenticated **before `accept()`** (no acceptance oracle, no pre-auth I/O). Two modes:
  (a) **Managed sandbox** — presents `X-Omnigent-Host-Token` (`OMNIGENT_HOST_TOKEN`); server
  resolves it via `host_store.resolve_launch_token` (SHA-256 digest, indexed equality → no
  timing oracle; expiry checked atomically; token scoped to one `host_id` → leaked token can't
  register arbitrary hosts). When present the user-token path is skipped entirely.
  (b) **User host** — mints a fresh **Databricks bearer** (or stored `omnigent login` token)
  via the runner's `_make_auth_token_factory`, **refreshed every reconnect** so long-lived
  hosts survive token expiry; the authenticated user is recorded as the host `owner`. Plus an
  `Origin: <internal-ws-origin>` header to pass the server's CSWSH guard. Cross-owner takeover
  of a `host_id` is refused with a real 409 before accept (`host_tunnel.py:177-202`), except on
  the single-user loopback server (`OMNIGENT_LOCAL_SINGLE_USER` re-owns in place).
- **Credential resolution / caching**: host bearer re-minted per reconnect (above);
  `configured_harnesses` recomputed off-loop on every (re)connect, but the **launch-time
  `harness_is_configured` check is authoritative** (`connect.py:766`, `:1484`). No long-lived
  credential cache in the host itself.

#### 7. Reliability gaps / sharp edges (confirmed in code)

- 🟠 **#1022 corporate-proxy gap — CONFIRMED.** Neither the host **daemon spawn env**
  (`_build_host_daemon_env`, `cli.py:2267`: allowlists `_RUNNER_ENV_ALLOWLIST` +
  `_LOCAL_DAEMON_ENV_ALLOWLIST`/prefixes locally, or `+DATABRICKS_` remotely) nor the
  **host→runner env** (`_RUNNER_ENV_ALLOWLIST`, `connect.py:203`) contains
  `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (grep for `PROXY` in `cli.py` returns only
  Apps-ingress mentions). So a user behind a corporate proxy: the foreground `omni host` would
  inherit the proxy from its shell, but the **backgrounded daemon** and **every spawned runner**
  silently lose it, with no config knob to re-add it (operator extras `OMNIGENT_RUNNER_ENV_PASSTHROUGH`
  only cover runner *credential* vars, not the daemon-spawn allowlist). Matches CUJ-ANALYSIS L504.
- ⚠️ **Tunnel-drop window drops launches.** A `host.launch_runner` sent while the tunnel is
  mid-reconnect throws `ConnectionError` from `send_text` → server rolls back the bind and
  returns 409/504 (`hosts.py:626-645`). The recycle heuristic (§5) exists *specifically* to
  shrink this window but it is URL-shape-dependent and best-effort.
- ⚠️ **`send_text` is loop-affine, not thread-safe.** `outbound_queue.put_nowait` is only safe
  because every current caller runs on the uvicorn loop; a future off-loop caller must use
  `call_soon_threadsafe` (`host_registry.py:303-324`). Latent footgun.
- ⚠️ **Per-replica registry.** A launch must be served by the replica holding the host tunnel;
  the `hosts` table is the only cross-replica truth, and `RunnerExitReports` live only on the
  tunnel's replica (`host_registry.py:61-64`) — a status poll that lands on another replica
  can't see the exit cause.
- ⚠️ **Result futures never time out on their own.** `pending_*` futures are only resolved by a
  matching result frame or popped by the *caller's* `wait_for` timeout; a buggy/oversized frame
  the server can't decode is dropped (`host_tunnel.py:545`) and the caller waits to its own
  timeout. Bounded by caller timeouts, but the dict entry lingers until then.
- 🟢 (mitigated) Runner that dies pre-connect: `_watch_runner` + `host.runner_exited` +
  `_on_runner_exited` mark the session failed with the real cause instead of a connect-timeout
  (`connect.py:898`, `app.py:2000`). This is the one good failure-signal path.

#### 8. Corrections to CUJ-ANALYSIS

CUJ-ANALYSIS covers the host **thinly** — essentially one bullet (the #1022 proxy gap, L504-505)
and scattered "host-launched" mentions. What's missing / to correct:

1. **L504-505 (#1022) is CORRECT but under-scoped.** It cites "`cli.py` daemon allowlist has no
   `HTTP(S)_PROXY`/`NO_PROXY`". Confirmed at `cli.py:2267` (`_build_host_daemon_env`) — but the
   gap is *two* allowlists: the host→**runner** env (`connect.py:203 _RUNNER_ENV_ALLOWLIST`) is
   equally proxy-blind, so even if the daemon had a proxy, spawned runners still wouldn't. The
   doc should name both layers.
2. **The entire host **control protocol** is absent from CUJ-ANALYSIS.** There is no description
   of the WS control-frame tunnel, the 16 `HostFrameKind`s, the `traceparent`-in-JSON
   propagation, the request/result `pending_*` future multiplexing, or the dual auth modes
   (managed-token vs user-bearer). This is the host's core and should be a first-class section
   alongside the runner-tunnel write-up.
3. **Managed-sandbox launch lifecycle is uncovered.** The arm-token-before-start race-closure
   (`managed_hosts.py:1888-1898`), durable-identity / ephemeral-box `relaunch`/`resume`
   distinction, and the `OMNIGENT_HOST_*` env-injected `omnigent host` exec inside the box
   (`base.py:319-326`) are all unmentioned. Any CUJ touching managed/web-UI sandboxes silently
   depends on this path. Also worth noting: the host's `session.id` is `None` on its own traces,
   so host activity won't appear under a conv's trace group — a gotcha for anyone tracing a
   managed session end-to-end.

---

### Executor & Harnesses

Scope: the inner `Executor` ABC + its 4 in-scope implementations (claude-sdk, codex-sdk,
claude-native, codex-native), the adapter that drives them, and the SDK-vs-native split.
**Polly = a custom agent that runs *on* one of these harnesses (typically claude-sdk) and
inherits that row** — it is not its own executor. All anchors below were opened and confirmed
against the worktree (`master-arch-docs`, main + telemetry PR #1617).

---

#### 1. Role & boundaries

The **Executor** is the leaf of the turn loop: given `(messages, tools, system_prompt, config)`
it yields a stream of `ExecutorEvent`s for ONE turn. It is the seam where Omnigent's abstract
message/tool model meets a concrete LLM/agent backend (`executor.py:518-537`).

It owns:
- Translating a backend's native output into the canonical `ExecutorEvent` hierarchy.
- Declaring *capabilities* (streaming, interrupt, queue, internal-tool-loop, context window) so
  the adapter/runner know how to drive it (`executor.py:541-587`).
- Per-session backend state (live SDK client / app-server thread / tmux bridge handle) and its
  teardown (`close_session`).

It does **NOT** own: durable history (the runner/server do), policy decisions (server, via the
adapter's policy-evaluator bridge), tool dispatch routing (the scaffold's `dispatch_tool`),
SSE/WS framing (the workflow's `_event_to_sse_dict`), or the request lifecycle. The executor is
called *inside* `ExecutorAdapter.run_turn` (`runtime/harnesses/_executor_adapter.py:394`), which
is itself driven by the harness scaffold/runner.

**The defining axis (SDK vs native):**
- **SDK harness** (claude-sdk, codex-sdk): an **in-process agent loop**. Omnigent owns the
  prompt + tool set + turn loop; the executor runs the vendor SDK/CLI as a child, bridges
  Omnigent tools in as MCP tools, and reconstructs events from the SDK message stream. Transcript
  is 100% Omnigent. `handles_tools_internally() == True` (`claude_sdk_executor.py:1611`,
  `codex_executor.py:2237`).
- **Native harness** (claude-native, codex-native): the executor does **not** run the model. A
  separate wrapper has already launched the *vendor* CLI/TUI (Claude Code / Codex app-server) in
  a tmux pane / on a UDS socket, with the vendor owning its own prompt + tool surface. The
  executor's `run_turn` only **injects the latest user message** into that resident process and
  returns; a separate always-on **forwarder** mirrors the vendor transcript back as
  `external_*` events. `supports_streaming() == False` for both
  (`claude_native_executor.py:64`, `codex_native_executor.py:60`) — output never flows through
  the executor.

---

#### 2. Key files & entrypoints (verified)

- `omnigent/inner/executor.py` — ABC + events.
  - `ExecutorConfig` (`:70-88`): `model`, `temperature`, `max_tokens`, `extra` (carries
    `reasoning_effort`, `max_tokens`).
  - Event hierarchy: `TextChunk:102`, `ReasoningChunk:114` (event_type ∈
    `reasoning_text`/`reasoning_summary`/`reasoning_started`), `ToolCallRequest:134`,
    `TurnComplete:150` (`response`/`continue_turn`/`usage`/`modified_by_policy`),
    `ToolCallStatus:178`, `ToolCallComplete:186` (emitted by Session, not executor — `:187`),
    `CompactionComplete:214` (`compacted_messages` = `None` when harness can't export, e.g.
    claude-sdk — `:227`), `TurnCancelled:237`, `ExecutorError:245` (`retryable` flag).
  - Base capability defaults (`:541-587`): `supports_streaming=False`, `supports_tool_calling=True`,
    `handles_tools_internally=False`, `max_context_tokens=None`, `interrupt_session→False`,
    `enqueue_session_message→False`, `supports_live_message_queue=False`,
    `supports_tool_boundary_interrupt=False`, `supports_stepwise_internal_turns=False`. **Every
    default is ❌/None except `supports_tool_calling`.** This matters: an executor that does NOT
    override a capability silently inherits the ❌.
  - Helpers: `split_transient_tail:357` (strips trailing `metadata.framework` notices so a
    history cursor doesn't skip them), `classify_tool_result:409` (status inference for
    ToolCallComplete), `iterate_blocking_stream:292` (sync-iterator→async bridge for codex).
- `omnigent/inner/claude_sdk_executor.py` (2682 lines) — `ClaudeSDKExecutor`. run_turn `:1817`;
  capabilities `:1605-1621`; `_get_or_create_client:1337` (mid-session `set_model` at `:1422`);
  `interrupt_session:1477`; `enqueue_session_message:1509`; gateway env `_resolve_gateway_env:719`.
- `omnigent/inner/codex_executor.py` (2381 lines) — `CodexExecutor`. run_turn `:2319`;
  capabilities `:2231-2241`; `interrupt_session:2243`; `enqueue_session_message:2276`;
  `_ensure_app_session:2294` (signature-keyed teardown/rebuild).
- `omnigent/inner/claude_native_executor.py` (258 lines) — `ClaudeNativeExecutor`. run_turn `:99`
  (tmux inject via `inject_user_message`, span `claude_native.inject` `:143`);
  `enqueue_session_message:72`; `_inject_lock:62` (serializes pane writes).
- `omnigent/inner/codex_native_executor.py` (434 lines) — `CodexNativeExecutor`. run_turn `:145`
  (UDS JSON-RPC `turn/start`/`turn/steer`); `interrupt_session:116` (`turn/interrupt` RPC);
  `enqueue_session_message:68` (`turn/steer`); `_model_effort_overrides:266` (`thread/settings/update`).
- `omnigent/runtime/harnesses/_executor_adapter.py` (the turn loop) — `run_turn:239`; builds
  `ExecutorConfig(model=request.model_override, extra={reasoning_effort, max_tokens})` `:273-284`;
  consume loop `:394`; interrupt-between-events `:407`; injection watcher `:363`.
- `omnigent/claude_native_bridge.py` — `inject_user_message:2393`, `inject_interrupt:2530`
  (sends `Escape`), `kill_session:2559`, `read/write_active_session_id:926/975`,
  `read_bridge_id:960`, `BRIDGE_ID_LABEL_KEY:70`.
- `omnigent/claude_native_forwarder.py` — `forward_claude_transcript_to_session:677`;
  `external_session_id` one-time mirror `_maybe_mirror_external_session_id:2350`;
  `_post_external_conversation_item:3352` (span `claude_native.forward` `:3377`);
  `_post_external_subagent_start:1115`, `_post_external_session_usage:3513`,
  `_post_external_model_change:3576`, `_post_external_compaction_status` (via `:2599`).
- `omnigent/reasoning_effort.py` — effort vocabularies (`:9-22`), `validate_effort:40`.
- `omnigent/cli.py` — `_dispatch_native_terminal_harness:5740` (called `:6049`).
- `omnigent/runtime/workflow.py` — default model resolution chain `:727-751`.

---

#### 3. Internal model (per executor)

**claude-sdk** (`_ClaudeClientState`, `claude_sdk_executor.py:563`): one persistent
`ClaudeSDKClient` per Omnigent session, keyed by `session_key`, cached in `self._clients`. Holds
`client`, current `model`, owning `loop`+`task` (so teardown can detect cross-loop close). Crashed
sessions blacklisted in `_crashed_sessions:1221`. Omnigent tools wired as in-process MCP tools
(`_build_mcp_tools:603`); bare names exposed as `mcp__omnigent__<name>` with a system-prompt bridge
note (`_augment_system_prompt_for_omnigent_mcp_tools:668`). Gateway path = base-URL +
`apiKeyHelper` refresh command + TTL (no static token snapshot — `_resolve_gateway_env:719`).

**codex-sdk** (`_CodexSessionState`): one Codex app-server `_CodexAppServerSession` per session,
keyed by a **signature** `(model, system_prompt, cwd, tool_signature)` (`:2346`). If the signature
changes between turns (e.g. `/model`), `_ensure_app_session:2294` **closes and rebuilds** the
app-server thread (`:2303-2304`) — this is why a mid-session model change "resets" the codex thread.

**claude-native** (`ClaudeNativeExecutor`): stateless except `_bridge_dir`,
`_request_session_id`, `_inject_lock`. No model, no SDK client. The vendor `claude` binary lives in
a tmux pane the wrapper launched; the executor only pastes keystrokes.

**codex-native** (`CodexNativeExecutor`): stateless except bridge dir + lock. Talks to the resident
Codex **app-server** over a UDS socket (`client_for_transport`) using JSON-RPC; reads/writes
`active_turn_id` in bridge state to decide `turn/start` vs `turn/steer`.

---

#### 4. Inter-component channels (with trace evidence)

The executor itself has no network edges — it is in-process inside `omni-harness` (SDK) or just
pastes into tmux / hits a local UDS (native). The *observable* edges are the runner↔server↔harness
flows the executor's behavior produces. **The SDK vs native trace contrast is the single clearest
signal of the architecture.**

##### SDK turn — conv_32db (claude-sdk, "create probe file, read, echo DONE")
Runner *drives* the harness; harness runs the whole loop in-process and calls *back* for policy.
```
omni-server POST /events ──(WS tunnel)──► omni-runner POST /v1/sessions/{conv}/events  [trace 11]
  omni-runner ─► omni-harness POST /v1/sessions/{conv}/events   (7754 ms — the WHOLE turn)
      omni-harness span agent:trace-probe  (7744 ms — in-process SDK agent loop)
      omni-runner ─► omni-server POST /policies/evaluate  phase=LLM_REQUEST   (model=null, 15 tools)
      omni-runner ─► omni-server POST /policies/evaluate  phase=LLM_RESPONSE  (text_preview…)
      [harness returns the final assistant text; runner persists it]
```
Edge counts (`summary_conv_32db…`): `omni-runner → omni-harness POST /events ×3` (the driving
events), policy phases are **LLM_REQUEST / LLM_RESPONSE** (the SDK executor's
`_policy_evaluator` bridge fires around the in-process LLM call). NO `external_*`, NO `/labels`.

##### Native turn — conv_94e6 (claude-native, same prompt)
Runner injects ONE message; the *forwarder* posts the transcript back as a cascade of events.
```
omni-runner POST /v1/sessions/{conv}/events  [trace 11]
  omni-runner ─► omni-harness POST /events  (2755 ms)
      omni-harness span agent:probe-claude-native (2739 ms)
      omni-harness span claude_native.inject   (2738 ms — the tmux send-keys, executor.run_turn)
  ── then the always-on forwarder mirrors the vendor transcript ──  [trace 4]
  omni-runner span claude_native.forward ×4  ─► omni-server POST /events  (external_conversation_item)
  omni-runner ─► omni-server GET /v1/sessions/{conv}/labels ×7   (reading bridge_id / active session labels)
  policy phases = REQUEST / TOOL_CALL / TOOL_RESULT  (the vendor's Bash call, evaluated via hook)
```
Edge counts (`summary_conv_94e6…`): `omni-runner → omni-server POST /events ×14` (forwarder
posting `external_*`), `GET /labels ×7`, but `omni-runner → omni-harness POST /events ×1` (one
inject). **This is the exact inverse of the SDK conv** — proving: SDK = runner→harness drives the
turn; native = vendor CLI runs in tmux, forwarder mirrors transcript, harness is barely touched.

**Why** (from code): claude-native `run_turn` (`claude_native_executor.py:99-153`) does one
`inject_user_message` (tmux send-keys) and yields `TurnComplete(response=None)` — there is no
streaming, so the harness produces almost nothing. The real model output is emitted by
`forward_claude_transcript_to_session` (`claude_native_forwarder.py:677`) reading the vendor's
transcript/JSONL and POSTing `external_conversation_item` (span `claude_native.forward`,
`:3377`). The `/labels` reads resolve the `bridge_id` (`BRIDGE_ID_LABEL_KEY`,
`claude_native_bridge.py:70`) so `--resume`/fork sessions land in the right tmux pane.

| edge | transport | SDK conv (32db) | native conv (94e6) |
|---|---|---|---|
| runner → harness `POST /events` | WS-tunnel→local HTTP | **×3 (drives turn)** | ×1 (one inject) |
| runner → server `POST /events` (`external_*`) | WS-tunnel | 0 | **×14 (forwarder)** |
| runner → server `GET /labels` | WS-tunnel | 0 | **×7 (bridge_id)** |
| runner → server `/policies/evaluate` | WS-tunnel | LLM_REQUEST/LLM_RESPONSE | REQUEST/TOOL_CALL/TOOL_RESULT |

---

#### 5. CUJ behaviors (per harness, with ⚠️ failure branches)

**Turn loop (all):** `ExecutorAdapter.run_turn:239` stamps every message with `session_key`
(`:271-272` — critical: without it the SDK keys its client under `"default"` and mid-turn
`enqueue` silently fails), builds `ExecutorConfig` `:284`, then `async for event in
executor.run_turn(...)` `:394`, translating each event to SSE via `_translate_event:437`.

**Interrupt (the corrected cell):** the adapter calls `executor.interrupt_session(session_key)`
between events when `ctx.cancelled` is set (`_executor_adapter.py:407`) AND from
`interrupt()` (`:521`).
- **claude-sdk** `interrupt_session:1477` → `client.interrupt()` (0.5 s timeout) **then always
  `close_session`** — the live client is dropped so the next turn rebuilds full history (incl. the
  runner's `[System: interrupted]` marker), because resumed turns only send the latest user
  message (`:1491-1497`). ✅ real.
- **codex-sdk** `interrupt_session:2243` → `app_session.interrupt_turn()` then `close_session`
  (resets thread_id, same rationale `:2260-2264`). ✅ real.
- **codex-native** `interrupt_session:116` → JSON-RPC `turn/interrupt` over the UDS socket. ✅ real.
- **claude-native** does **NOT override `interrupt_session`** → inherits base `False`
  (`executor.py:569`), so the adapter's call is a no-op. The Stop button works through a
  **separate bridge path**: server route → `runner/app.py:10518` →
  `claude_native_bridge.inject_interrupt:2530` which sends a single **`Escape`** keystroke to the
  tmux pane (Claude Code's TUI cancels on Escape). ⚠️ this is the cell the prior pass got wrong —
  "Stop works" is TRUE but "executor.interrupt_session exists" is FALSE; the two are independent.

**Mid-turn queue / steer (all four ✅, different mechanism):**
- claude-sdk `enqueue_session_message:1509` → `client.query(prompt)` into the live session.
- codex-sdk `:2276` → `app_session.enqueue_message`.
- claude-native `:72` → `inject_user_message` (tmux send-keys), serialized under `_inject_lock`.
- codex-native `:68` → `turn/steer` RPC (reads `active_turn_id`, serialized under `_inject_lock`).
  ⚠️ both native paths share the lock with `run_turn` because the steer/start decision is a
  read-decide-RPC-write that races with the initiating inject (see
  `designs/NATIVE_INJECTION_SERIALIZATION.md`); without it keystrokes interleave ("1"+"2"→"12").

**Model & effort at session start:** resolved in `workflow.py` *before* spawning the harness:
spec `executor.model` → provider `models.default` (`family.default_model`) → bundled catalog
family default (`_catalog_default_model`) → **fail loud** (`:729-750`). Written into the
`HARNESS_*_MODEL` env the executor reads as its `_model_override`. Start-time effort comes from the
NewChatDialog → request.reasoning.effort → `config.extra["reasoning_effort"]`.

**Model & effort mid-session (per harness — mechanism differs):**
- claude-sdk: `cfg.model or self._model_override` (`:1910`); applied to the **live** client via
  `set_model` when `state.model != model` (`:1422-1424`) — no teardown. Effort →
  `options_kwargs["effort"]` + adaptive thinking (`:2011-2033`). ✅ applies without restart.
- codex-sdk: model is part of the app-session **signature** (`:2346`); a change rebuilds the
  app-server thread (`_ensure_app_session:2303`). ⚠️ effective but the codex thread state resets.
- codex-native: `/model` pick → `config.model` + `config.extra["reasoning_effort"]` →
  `_model_effort_overrides:266` → a `thread/settings/update` RPC *before* the bare `turn/start`
  (`:237-251`) — `turn/start` itself takes no model/effort. ✅ persists for this+later turns.
- claude-native: the executor ignores `config` entirely (`run_turn` `del tools, system_prompt,
  config` `:123`); the vendor TUI owns its model. A web `/effort` is mirrored in-pane and the
  statusLine change is reflected back by the forwarder. ⚠️ next-turn / vendor-controlled only.

**Default provider/credential resolution:** spec `executor.auth` block → env → CLI login → ambient
detection. claude-sdk gateway path derives base-URL + `apiKeyHelper` refresh *command* (not a
token) from `~/.databrickscfg` (`_resolve_gateway_env:719`, `_databricks_claude_auth_command:819`)
so the CLI re-mints tokens on long sessions (avoids the 1 h-snapshot fail-closed bug). [→ Auth section]

**Harness switching (`POST /sessions/{id}/switch-agent`, `sessions.py:15416`):** loads the target
bundle before mutating (`:15519`); model_override/reasoning_effort carry over **only within the
same provider family** (`_same_provider_family:15537`); a **native target clears
`external_session_id`** and drops the fork-source directive so the runner rebuilds the native
transcript from this session's own Omnigent items (`:15532-15536`). cursor-native can't replay fork
history (no resumable session file) so it isn't promised one (`:15544`). [Polly switches like any
agent — it's just a bundle.]

**Native bridging (one-time external_session_id + bridge labels + forwarder):**
- `external_session_id` is set **exactly once** via `PATCH /sessions/{id}` (`sessions.py:15144`);
  the store raises `ValueError` on overwrite → 400 INVALID_INPUT (`:15158-15166`). The forwarder
  mirrors it best-effort once per (re)start: `_maybe_mirror_external_session_id:2350` guarded by
  `external_session_id_mirrored` flag (`forwarder:752`), reset to False on `/clear`
  (`:810,846`).
- `bridge_id` is stored as a session **label** (`BRIDGE_ID_LABEL_KEY`,
  `claude_native_bridge.py:70`); fork/clear/resume keep the SAME bridge_id (`:628`) so they share
  one tmux pane — this is why the native conv reads `/labels ×7`.
- The forwarder emits `external_{conversation_item,subagent_start,session_usage,model_change,
  compaction_status}` — the native analog of the SDK executor's in-process event stream.

**Subagents:** gated by the **tool surface**, not an executor flag. SDK harnesses bridge
`sys_session_send`/`sys_session_create` as MCP tools (the model calls them → child session). The
subagent conv_fc47 (claude-sdk) is in the corpus. claude-native surfaces subagents via the
vendor's Task tool → forwarder `_post_external_subagent_start:1115`. codex (both) = implicit via
`CODEX_HOME`/subprocess isolation. [Depth limits / dispatch → Subagents section.]

---

#### 6. Answers to doc questions (terse, code-anchored)

- **Executor's role in the turn loop:** the per-turn event generator at the bottom of
  `ExecutorAdapter.run_turn`; everything above it (history, policy, SSE, dispatch) is the
  adapter/scaffold/runner. `executor.py:524`, driven at `_executor_adapter.py:394`.
- **SDK vs native taxonomy & who owns what:** SDK = in-process loop, Omnigent owns
  prompt+tools+transcript, `handles_tools_internally=True`. Native = resident vendor CLI in
  tmux/UDS, vendor owns prompt+tools, executor only injects, forwarder mirrors transcript,
  `supports_streaming=False`.
- **Per-harness capability matrix:** see §7 below (cell-by-cell verified).
- **Model/effort resolution start+mid:** start in `workflow.py:727-750`; mid via per-harness
  mechanism above (sdk live `set_model`; codex-sdk thread rebuild; codex-native
  `thread/settings/update`; claude-native vendor-only).
- **Default model/provider chain:** spec model → provider default → catalog default → fail loud
  (`workflow.py:729-750`); executor adds a per-turn `/model`-override layer on top.
- **Harness switching:** `switch-agent` clears `external_session_id` for native targets, carries
  model settings only within a provider family (`sessions.py:15416,15532`).
- **Reasoning-effort vocab (source of truth `reasoning_effort.py`):**
  `CLAUDE/ANTHROPIC = {low,medium,high,xhigh,max}` (`:13`),
  `CODEX/OPENAI = {none,minimal,low,medium,high,xhigh}` (`:12`). `EFFORT_CLEAR_VALUES =
  {default,off,reset}` (`:10`).

---

#### 7. Corrected per-harness capability matrix (cell-by-cell vs code)

Two levels reported because the prior pass conflated them:
**(A) executor-method level** — does the executor *override* the capability (else inherits base ❌)?
**(B) product level** — does the user-facing behavior work (possibly via a non-executor path)?

| Harness | interrupt — exec method | interrupt — product (Stop) | queue (`supports_live_message_queue`) | tool-boundary interrupt | subagents | reasoning-effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|---|---|
| **claude-sdk** | ✅ `interrupt()`+close (`:1477`) | ✅ | ✅ (`:1614`) | ✅ (`:1617`, the **only** harness) | ✅ (`sys_session_*` MCP) | ✅ {low,med,high,xhigh,max} (`:2011`) | ✅ (`_can_use_tool` → elicitation, `:1633`) | ✅ live `set_model` (`:1422`) |
| **codex-sdk** | ✅ `interrupt_turn()`+close (`:2243`) | ✅ | ✅ (`:2240`) | ❌ **not overridden → base False** | ⚠️† subprocess `CODEX_HOME` | ✅ {none,minimal,low,med,high,xhigh} (`:2353`) | ⚠️‡ executor base ❌; forwarder may handle | ⚠️ rebuilds thread (`:2303`) |
| **claude-native** | ❌ **not overridden → base False (no-op)** | ✅ via bridge `inject_interrupt` Escape (`bridge:2530`, called `runner/app.py:10518`) | ✅ tmux inject (`:72`) | ❌ base False | ✅ vendor Task → `external_subagent_start` | ⚠️ vendor-only (`/effort` mirrored, next turn) | ✅ via hook/policy + vendor UI | ⚠️ vendor-only, next turn (config ignored, `:123`) |
| **codex-native** | ✅ `turn/interrupt` RPC (`:116`) | ✅ | ✅ `turn/steer` (`:68`) | ❌ base False | ⚠️† subprocess isolation | ✅ {…openai} via `thread/settings/update` (`:266`) | ✅ forwarder hook | ✅ `thread/settings/update` (`:237`) |

Legend: ✅ confirmed · ⚠️ partial/caveated · ❌ confirmed absent.
† codex subagents = implicit subprocess `CODEX_HOME` isolation, not a declared capability.
‡ codex-sdk elicitation: executor returns base ❌; the forwarder *may* handle it but unverified at
the executor boundary (codex-*native* elicitation IS ✅ via the forwarder hook).
**Polly** has no row — running on claude-sdk it reads exactly as the claude-sdk row.

Key cross-cell facts the §4 matrix flattens: only **claude-sdk** declares
`supports_tool_boundary_interrupt` (`:1617`); codex-sdk does not (so queued input can't be applied
at a tool boundary — base False). `handles_tools_internally=True` only for the two SDK rows; both
native rows leave it base False (the vendor runs its own tools, mirrored). `max_context_tokens`:
claude-sdk returns `None` "SDK manages its own context" (`:1620`).

---

#### 8. Reliability gaps / sharp edges (confirmed in code)

1. **Base-default trap:** any executor that forgets to override a capability silently inherits ❌
   (`executor.py:541-587`). This is exactly why claude-native's Stop *looked* broken to a prior
   reader — `interrupt_session` falls through to the base no-op; the working path is the bridge
   Escape, an entirely separate wire. Reviewing the matrix from executor methods alone is wrong.
2. **session_key stamping is load-bearing:** if the adapter didn't stamp `message["session_id"]`
   (`_executor_adapter.py:271`), the SDK keys its client under `"default"` and every mid-turn
   `enqueue_session_message` returns False — the user-reported "steering ignored, Claude keeps
   going" symptom (documented inline `:267-270`).
3. **Interrupt drops the live session (both SDK):** claude-sdk and codex-sdk *always*
   `close_session` after an interrupt (`:1499`, `:2266`) because a resumed transcript would only
   resend the latest user message and bypass the `[System: interrupted]` marker — so an interrupt
   pays a full cold-start + history-replay on the next turn.
4. **claude-native interrupt is best-effort Escape with a 1 s tmux timeout** (`runner/app.py:10518`);
   if `tmux.json` isn't advertised yet it 503s and the Stop is silently a no-op. There's also no
   `[System: interrupted]` marker appended for native (`runner/app.py:10527` comment).
5. **codex-sdk mid-session model = thread teardown** (`:2303`): a `/model` mid-conversation throws
   away the running codex app-server thread (signature change), unlike claude-sdk's in-place
   `set_model`. Cost + potential loss of in-thread state.
6. **codex-native double-start race** is only prevented by `_inject_lock`
   (`codex_native_executor.py:58`); the read-decide(start vs steer)-RPC-write is not atomic
   otherwise → two injects could both see "no active turn" and double-start.
7. **`external_session_id` overwrite = hard 400** (`sessions.py:15158`): if a wrapper bridge ever
   re-PATCHes a different vendor session id (e.g. after an unexpected vendor relaunch with a new
   id), the write fails closed rather than re-binding. The forwarder's mirror flag resets on
   `/clear` (`forwarder:810`) but the server side does not, so a post-`/clear` id must match the
   first or be cleared via switch-agent/fork.
8. **CompactionComplete.compacted_messages = None on claude-sdk** (`executor.py:227`): the CLI's
   compaction is internal, so a resumed claude-sdk session can't replay pre-compacted history the
   way harnesses that export their compacted state can. [→ Compaction section.]

---

#### 9. Corrections to CUJ-ANALYSIS

- **§4 matrix, claude-native interrupt cell** — the matrix is now CORRECT (✅ via bridge
  `inject_interrupt`) but the **anchor is stale**: it cites `claude_native_bridge.py:2484`; the
  actual definition is **`:2530`**, and the caller is `runner/app.py:10518` (server route). The
  note's reasoning (executor method ≠ Stop button) is right and verified — claude-native does NOT
  override `interrupt_session` (inherits base False, `executor.py:569`).
- **§4 matrix — missing the tool-boundary-interrupt divergence.** The matrix has no column for it,
  but it's a real per-harness difference: **only claude-sdk** overrides
  `supports_tool_boundary_interrupt` (`:1617`); codex-sdk, claude-native, codex-native all inherit
  base False. Worth a footnote so "queue" isn't read as "queued input applies mid-tool".
- **§2.B / §4, "codex (SDK) mid-session model: per-turn (resets at session)"** — accurate but
  under-specified. The mechanism is an **app-server thread teardown+rebuild** keyed on the
  `(model, system_prompt, cwd, tools)` signature (`codex_executor.py:2303,2346`), not a soft
  per-turn config. Contrast claude-sdk, which mutates the **live** client via `set_model`
  (`:1422`) with no teardown — the §4 "Notes" lump these together as "SDK set_model/per-turn
  config" but they behave differently (claude-sdk keeps session state, codex-sdk loses it).
- **§2.B claude-native mid-session model "✅ (next turn)"** is optimistic: the executor *ignores*
  `config` outright (`claude_native_executor.py:123` `del tools, system_prompt, config`). The
  vendor TUI owns the model; a web change only takes effect insofar as it's mirrored in-pane via
  `/effort`/statusLine — there is no executor-side application. Reads better as ⚠️ vendor-only.
- **§2.B `_dispatch_native_terminal_harness` anchor `cli.py:5740`** — confirmed correct
  (definition at `:5740`, called `:6049`). No drift. (Recorded as a positive confirmation.)

---

### Policy & Elicitations

> Guardrails, the in-process policy engine, approval (ASK) gates, and the per-harness
> hooks that make policies enforceable. All `path:line` below were opened & confirmed in
> the `master-arch-docs` worktree (main + telemetry PR #1617). `(unverified)` tags claims
> I could not pin to code.

#### 1. Role & boundaries

The policy subsystem decides **ALLOW / ASK / DENY** at five enforcement phases and resolves
human approvals (elicitations). It owns:
- The **in-process engine** `PolicyEngine` (`omnigent/runtime/policies/engine.py:43`) — the single
  choke point; every eval routes through `evaluate()` which wraps a `policy.evaluate` telemetry span.
- **Policy resolution / composition** (DENY short-circuit, ASK accumulation, label/state side-effects).
- The **ASK / elicitation flow** (server-side held gate + in-process workflow gate), the web
  `ApprovalCard` wire contract, and the resolve→Future→forward plumbing.
- The **per-harness hooks** that surface tool calls / prompts to the engine, and the
  **server held-response (long-poll)** that returns an ASK verdict to a native hook.
- The **handler allowlist** (RCE guard) for user-attached policies.

It does **NOT** own: the harness's own consent gate (Claude `PermissionRequest` web routing is a
*separate* gate — see §6), tmux/keystroke delivery (only out-of-scope native harnesses use that),
sandboxing (OmniBox; policies are a safety net, not a security boundary — `nessie/policies.py:142`),
or credential minting (it consumes the auth-header factory, §5).

#### 2. Key files & entrypoints (verified)

| File:line | What |
|---|---|
| `omnigent/runtime/policies/engine.py:43` | `PolicyEngine`; `evaluate()` @ `:230` (span wrap), `_evaluate_composed()` @ `:284` (DENY short-circuit + ASK accumulate) |
| `engine.py:1038` | `_fail_closed()` — the 3-way fail policy (ALLOW/ASK/DENY by declared action list) |
| `engine.py:976` | `_dispatch_policy()` — wraps every `policy.evaluate` in try/except → `_fail_closed` |
| `omnigent/runtime/policies/approval.py:80` | `_await_elicitation()` — in-process workflow ASK gate (registers `__elicitation__` row, parks on `tool_result` topic) |
| `approval.py:175` | `build_elicitation_request_event()` — the `response.elicitation_request` wire shape (MCP `ElicitRequestFormParams`); URL mode → `/approve/{sid}/{eid}` |
| `approval.py:290` | `_parse_verdict()` — strict: only `action=="accept"` → True (fail-closed) |
| `omnigent/runtime/policies/enforcement.py:21` | `_enforce_policy()` — thin call-site wrapper used by the 4 workflow sites |
| `omnigent/runner/policy.py:109` | `RunnerToolPolicyGate` — runner MCP fast-path (function-type TOOL_CALL/TOOL_RESULT only) |
| `omnigent/native_policy_hook.py` | **shared** claude+codex hook conversion: hook payload→`EvaluationRequest`, `EvaluationResponse`→hook output, `post_evaluate_with_retry`, `fail_closed_hook_output`, `policy_hook_reauth` |
| `omnigent/claude_native_hook.py:73` | claude policy hook `main()`; `permission-request` subcmd @ `:84` (separate gate); `_PERMISSION_TIMEOUT_S=86400` @ `:46` |
| `omnigent/codex_native_hook.py:43` | codex policy hook `main()` (same `PreToolUse/PostToolUse/UserPromptSubmit` shape) |
| `omnigent/codex_native_forwarder.py:3007` | `_handle_codex_elicitation_request` — codex's *own* permission prompt (`item/tool/requestUserInput`) → `/hooks/codex-elicitation-request` |
| `omnigent/codex_native_elicitation.py:24` | `codex_elicitation_id()` — deterministic id so `serverRequest/resolved` clears the right card |
| `omnigent/server/routes/session_policies.py:148` | `POST /v1/sessions/{id}/policies` (session create); registry allowlist guard @ `:181` |
| `omnigent/server/routes/default_policies.py:129` | `POST /v1/policies` (admin default); `_require_admin` @ `:70` |
| `omnigent/server/routes/sessions.py:15964` | `POST /sessions/{id}/policies/evaluate` (the native-hook + SDK-relay eval endpoint) |
| `sessions.py:4119` | `_hold_native_ask_gate()` — server-side held ASK gate (TOOL_CALL/LLM_REQUEST/REQUEST) |
| `sessions.py:1397` | `_publish_and_wait_for_harness_elicitation()` — parks the server-side Future, 3-way race |
| `sessions.py:18014` | `POST /sessions/{id}/elicitations/{eid}/resolve` route |
| `sessions.py:3978` | approval dispatch: `_harness_elicitation_registry[eid].set_result(...)` (the Future resolution) |
| `sessions.py:10801` | `_evaluate_input_policy()` — REQUEST gate at `POST /events` (SDK/web path; bypassed for native) |
| `sessions.py:10556` | `_evaluate_tool_call_policy()` (server-side relay tool gate) |
| `omnigent/runner/app.py:6248` | `_evaluate_policy_via_omnigent()` — SDK relay: `policy_evaluation.requested` SSE → POST evaluate → `policy_verdict` event |
| `omnigent/runtime/pending_elicitations.py` | in-memory index of outstanding elicitations (sidebar badge + cold-load replay) |
| `omnigent/policies/registry.py:156` | `is_registered_handler()` — the RCE allowlist |
| `omnigent/inner/nessie/policies.py:346` | `blast_radius`, `spawn_bounds` (`:408`), `worktree_guard` (`:520`), `read_only_os` (`:572`) — FunctionPolicy factories, run runner-side |
| `omnigent/policies/builtins/` | `safety.py`, `cost.py`, `risk_score.py`, `routing.py` (classifier `deny_trivial_to_expensive_model`), `prompt.py`, `github.py`, `google.py`; modules scanned via `BUILTIN_POLICY_MODULES` (`builtins/__init__.py:37`) |
| `omnigent/tools/builtins/policy.py:33` | `sys_add_policy` MCP tool → `POST /v1/sessions/{id}/policies`; `sys_policy_registry` reads `/v1/policy-registry` |

#### 3. Internal model

**`PolicyEngine`** — per-workflow, plain object built at top of `_run_agent_loop` (no ContextVar).
Holds `policies: list[Policy]` in **YAML declaration order**, a hot **label cache** (`_labels`,
write-through to `conversation_labels`), `_session_state` (write-through to `session_usage`/state),
cumulative `_usage` + optional `_subtree_usage` / `_user_daily_cost`, resolved `_model`, and an
optional `_llm_client`. State is snapshotted **at construction** — a fresh `build_policy_engine`
is the only way to see a sibling's just-recorded approval (`sessions.py:16087` `_build_engine`).

**`Policy` kinds** (`omnigent/policies/`): `FunctionPolicy` (dotted-path callable, evaluated runner- or
server-side), `PromptPolicy` (LLM classifier), `LabelPolicy`. Spec carries `on:` (PhaseSelector list),
`condition:` (label gate), `action:` whitelist, `set_labels:` whitelist, `ask_timeout` override.

**`Phase` enum** (`spec/types.py:1074`): `REQUEST · TOOL_CALL · TOOL_RESULT · LLM_REQUEST · LLM_RESPONSE`
(values `request/tool_call/tool_result/llm_request/llm_response`). Tool-name narrowing only valid on
TOOL_CALL/TOOL_RESULT (`spec/types.py:1189`). Proto map `_PROTO_EVENT_TYPE_TO_PHASE` @ `sessions.py:15946`.

**`PolicyResult`** = `action` + `reason` + `set_labels` + `state_updates` + `deciding_policies` + `data`
(content-rewrite payload, e.g. PII redaction). DENY carries one `deciding_policy`; ASK carries the full
`deciding_policies` list (reasons joined `"; "`).

#### 4. Composition & fail-open/closed

**Composition** (`engine.py:284` `_evaluate_composed`): iterate policies in YAML order; per policy:
skip if `PhaseSelector` no-match (`_should_fire` @ `:457`) or `condition:` label-gate no-match
(`_condition_matches` @ `:1237`, AND across keys / OR within a list); else dispatch. Then:
1. **DENY → short-circuit immediately** (`_compose_deny` @ `:414`) — applies accumulated writes from
   ALLOWing predecessors + the DENYer's own, returns DENY. No later policy can override.
2. **ASK accumulates** (does not short-circuit) — a later policy may still DENY. After the loop, if any
   ASK: return ASK carrying **withheld** `set_labels`/`state_updates` (applied only on approve, §7.2).
3. Else ALLOW (apply accumulated writes).
4. **Data chaining**: a policy returning `data` feeds it forward as the next policy's `ctx.content`
   (`engine.py:382`) — sequential transform (e.g. redact then classify).
- Monotonic label merge across one eval: `_merge_monotonic_writes` @ `:1117` keeps the most-restrictive
  value per the LabelDef direction (a later policy can't lower an `increasing` taint label).

**Fail policy** — two layers, both phase-aware:

| Layer | Mechanism | TOOL_CALL | REQUEST | TOOL_RESULT | LLM_REQUEST | LLM_RESPONSE |
|---|---|---|---|---|---|---|
| **Per-policy exception** (`_fail_closed` engine.py:1038) | depends on declared `action:` list, NOT phase | DENY (default) / ALLOW if `[allow]` classifier-only / ASK if `[ask]`/`[allow,ask]` | same | same | same | same |
| **Eval unreachable** (native hook `fail_closed_hook_output`; runner relay `_evaluate_policy_via_omnigent`) | phase-aware, keyed on `FAIL_CLOSED_PHASES=("PHASE_TOOL_CALL","PHASE_REQUEST")` (`policies/types.py:61`) | **CLOSED → deny** | **CLOSED → block** | **OPEN → None** | **OPEN → allow** | **OPEN → allow** |

Key nuance the CUJ table flattens: **a broken/raising policy** is *not* purely fail-closed — a
classifier-only `[allow]` policy substitutes ALLOW (honours "never blocks"), an `[ask]`/`[allow,ask]`
gate substitutes ASK; only DENY-capable / no-list policies fail-closed DENY (`engine.py:1062-1089`).
"Fail-CLOSED on TOOL_CALL/REQUEST" applies to the **transport-unreachable** path (`FAIL_CLOSED_PHASES`),
not to a policy that throws.

#### 5. Inter-component channels (in/out)

```
                          (TUI types prompt)          (web prompt — already gated)
                                  │                              │
 [harness]                       UserPromptSubmit hook       POST /events (_evaluate_input_policy)
   PreToolUse/PostToolUse ───────────┐                           │  REQUEST gate (SDK/web only)
   (native, claude+codex)            ▼                           ▼
                    POST /v1/sessions/{id}/policies/evaluate  ─────────────► [omni-server] PolicyEngine.evaluate
 [harness SDK]   policy_evaluation.requested (SSE)                                  │ policy.evaluate span
   ──► [runner] _evaluate_policy_via_omnigent ──POST evaluate──┘                    │
   ◄── policy_verdict (inbound event)                                               │ ASK?
 [runner MCP]   RunnerToolPolicyGate (fast-path ALLOW/DENY) ── ASK → POST evaluate ─┘
                                                                                    ▼
                                                            _hold_native_ask_gate / _await_elicitation
                                                                                    │
                                            publish response.elicitation_request (SSE) ──► web ApprovalCard / REPL
                                            parks server-side Future / tool_result topic
   web APPROVE ── POST /elicitations/{eid}/resolve ──► dispatch (sessions.py:3978) set_result(Future)
                                            also publishes response.elicitation_resolved + forwards approval→runner
```

- **harness ⇄ server**: REST `POST /policies/evaluate` (native hooks; the SDK relay). The ASK verdict
  is returned **in the held HTTP response body** (long-poll): the POST blocks until a human resolves
  (`read_timeout` ≈ 1 day, `_EVALUATE_POLICY_TIMEOUT_S=86400`), then the response is a *hard* ALLOW/DENY —
  the hook **never sees ASK** (`sessions.py:16125-16174`). **Trace evidence** (`conv_eb24…`, policy-guard):
  edge `omni-runner → omni-server [POST /v1/sessions/{id}/policies/evaluate] x1`, two `policy.evaluate`
  spans on `omni-server` capturing REQUEST content (`"Use the shell to run exactly: echo hello…"`) and
  LLM_REQUEST content (`{"messages_count":1,"tools_count":15,"system_prompt_preview":…}`). In `conv_32db…`
  (sdk-tools) a third span captures LLM_RESPONSE (`text_preview` + `usage`).
- **runner ⇄ server (SDK relay)**: harness emits `policy_evaluation.requested` SSE → runner POSTs evaluate
  → verdict back to harness as `policy_verdict`. Same endpoint, different caller.
- **server → clients**: SSE `response.elicitation_request` / `response.elicitation_resolved`.
- **client → server**: REST `POST /elicitations/{eid}/resolve` (or a session `type=approval` event on
  `POST /events`) carrying MCP `ElicitationResult` `{action, content?}`.
- **server → runner**: approval forwarded as canonical `approval` event (`_forward_approval_to_runner`
  `sessions.py:3880`) so a runner-parked `pending_approvals` Future resolves.
- **hook ⇄ server auth**: hook wrapper bakes one-shot bearer (`policy_hook_wrapper_script`
  `native_policy_hook.py:103`); on 401/302-to-`/oidc/` it re-mints once via `policy_hook_reauth` (`:133`).

#### 6. Required hooks per harness (the "make ALL policies work" question)

A harness must expose enough hooks that the engine sees **every** policy-relevant event (request,
tool call, tool result) *and* can deliver an ASK verdict. The Omnigent **policy** gate and the user's
**own consent** gate are deliberately separate (`native_policy_hook.py:285-315`): the policy hook returns
"no opinion" (`None`) on ALLOW so the harness's native permission prompt — and, for Claude, the
`PermissionRequest`→web routing — still fires.

| Harness | Hooks it MUST expose for all policies | REQUEST gate | TOOL_CALL gate | TOOL_RESULT | ASK verdict delivery |
|---|---|---|---|---|---|
| **claude-native** | (1) `UserPromptSubmit`+`PreToolUse`+`PostToolUse` → `/policies/evaluate` (policy gate); (2) `PermissionRequest` → `/hooks/permission-request` (separate **consent** gate, day-long long-poll) | `UserPromptSubmit` (sole REQUEST gate; server `_evaluate_input_policy` bypassed for native) | `PreToolUse` | `PostToolUse` (warn-only) | **long-poll HTTP held response** (verdict in `/policies/evaluate` body) |
| **codex-native** | (1) `PreToolUse`/`PostToolUse`/`UserPromptSubmit` → `/policies/evaluate` (same shared hook code); (2) forwarder relays codex's own `item/tool/requestUserInput` → `/hooks/codex-elicitation-request` | `UserPromptSubmit` | `PreToolUse` | `PostToolUse` | **long-poll HTTP held response** (policy ASK); codex's own permission prompt resolves via `serverRequest/resolved` (deterministic `elicit_codex_…` id) |
| **claude-sdk** | runner relay only — harness emits `policy_evaluation.requested` SSE; runner posts evaluate. Plus `RunnerToolPolicyGate` MCP fast-path for function-type tool policies | runner relay / server `_evaluate_input_policy` at `POST /events` | `RunnerToolPolicyGate` fast-path → server escalation; relay for LLM phases | relay (fail-open) | **`approval` event** → runner `pending_approvals` Future (`runner/policy.py:23`) |
| **codex (sdk)** | same as claude-sdk (relay + MCP fast-path) | server `_evaluate_input_policy` | fast-path / relay | relay | **`approval` event** → runner Future |
| **Polly** | inherits its underlying harness's row (Polly = custom agents on a harness) | per harness | per harness | per harness | per harness |

- **mcp__omnigent__\*** tools are skipped by the native hook (`native_policy_hook.py:244`) — already
  gated by the relay path (`ProxyMcpManager` → `/mcp` → `_evaluate_tool_call_policy`). **Connector-native**
  `mcp__github__*` etc. still go through the hook.
- **No keystroke emulation** for any in-scope harness (grep of claude/codex hooks: empty). Verdicts come
  back via held HTTP (native) or `approval` event (SDK). Out-of-scope native harnesses (goose/hermes/pi)
  use tmux-keystroke / external-resolved mirrors — not relevant here.

#### 7. The ASK flow end-to-end

Two parking mechanisms, same wire shape:
- **Server-side held gate** `_hold_native_ask_gate` (`sessions.py:4119`) — for native PreToolUse + the
  REQUEST input gate (no runner in the loop yet). Publishes `response.elicitation_request`, parks the
  Future via `_publish_and_wait_for_harness_elicitation` (`:1397`), returns a bool. Wait ends on first of:
  (1) web verdict Future, (2) terminal-resolved Event (a mirrored tool result proves the TUI answered),
  (3) disconnect/timeout. Only (1) yields accept; (2)/(3) → DENY (fail-ask).
- **In-process workflow gate** `_await_elicitation` (`approval.py:80`) — registers a `__elicitation__`
  pending-tool row, emits on the root task SSE, parks on the `tool_result` topic.

End-to-end (native TOOL_CALL ASK):
```
PreToolUse hook ─POST /policies/evaluate─► engine.evaluate → ASK
  → _native_ask_gate_lock(session, deciding_policy)   # serialize siblings hitting same checkpoint
  → rebuild engine + re-evaluate under lock (a sibling's approval may have collapsed it)
  → still ASK → _hold_native_ask_gate
       → publish response.elicitation_request (mode:"url" → /approve/{sid}/{eid})  ──► web ApprovalCard
       → park Future
  web APPROVE → POST /elicitations/{eid}/resolve → _harness_elicitation_registry[eid].set_result(accept)
       → publish response.elicitation_resolved (badge clears, sidebar count--)
       → forward approval → runner
  gate returns True → apply withheld set_labels/state_updates (POLICIES.md §7.2) → hard ALLOW in held body
```
- **`ask_timeout` → DENY**: `resolve_ask_timeout` (`approval.py:264`, per-policy override else engine
  default); on expiry the Future race returns None → `_hold_native_ask_gate` returns False → DENY.
  Per-session/per-user/subtree cost approvals are routed to the **root** conversation /
  user-daily store so one approval covers the spawn tree (`engine.py:574`, `:599`).
- **TOOL_RESULT ASK is collapsed to DENY** in the runner fast-path (`runner/policy.py:184`) — the output
  already exists, no clean rollback.
- **DENY-on-deny invariant**: on decline/cancel/timeout/malformed verdict, withheld writes are dropped —
  a denied ASK leaves no trace (`approval.py:144`, `_hold_native_ask_gate:4212`).

#### 8. Read-only (LEVEL_READ) eval

`POST /policies/evaluate` computes `is_read_only = level < LEVEL_EDIT` (`sessions.py:16005`) and calls
`engine.evaluate(ctx, read_only=True)`. Read-only path (`engine.py:284`, the `read_only` branches):
policies still run and the composed result still carries `set_labels`/`state_updates`, but **nothing is
persisted** and a read-only caller **never enters the ASK gate** (`sessions.py:16130`) — parking would
create an elicitation (a mutation). Lets a `LEVEL_READ` collaborator audit "what would be denied".

#### 9. Policy creation & enforcement levels

- **Session-level**: `sys_add_policy` tool → `POST /v1/sessions/{id}/policies` (`session_policies.py:148`),
  `source="session"`, requires `LEVEL_EDIT`. Handler validated against the **registry allowlist**
  (`is_registered_handler`, `:181`) — an unregistered dotted path is rejected (RCE guard; admin must add
  the module via `policy_modules`). `factory_params` validated against the registry schema. Dup name → 409,
  bad params → 400. `type` is **immutable** on PATCH (`:285`). Activates on next engine build.
- **Admin / server default**: `POST /v1/policies` (`default_policies.py:129`, `_require_admin`),
  `session_id=NULL`, applies server-wide; `GET` is read-only-authenticated. Surfaced in the session list
  with `source="admin"` (`session_policies.py:239`).
- **Spec-declared**: agent YAML `guardrails.policies:`, `source="spec"`, `id=None`, **immutable**
  (cannot PATCH/DELETE — `_spec_to_response` `:72`).
- **Enforcement levels**:
  - *Server*: the authoritative engine. REQUEST (SDK/web at `POST /events`), TOOL_CALL/TOOL_RESULT relay,
    **LLM_REQUEST/LLM_RESPONSE (server-only, advisory, fail-open)**, and the elicitation registry all live
    server-side. The native hook posts here for every native event.
  - *Runner*: `RunnerToolPolicyGate` (`runner/policy.py`) is a **fast-path for function-type
    TOOL_CALL/TOOL_RESULT only** — ALLOW/DENY decided locally before MCP dispatch; **ASK escalates** to the
    server (`evaluate_policy=True`) which owns the elicitation channel. `label`/`prompt` types stay
    server-side (need the store / LLM classifier). The dual eval is intentional.

#### 10. Reliability gaps / sharp edges (confirmed in code)

1. **LLM_REQUEST/LLM_RESPONSE have no runner-local gate** — only enforced if the harness emits
   `policy_evaluation.requested` (SDK relay) or via the native hook posting those phases; and they
   **fail-OPEN** on any outage (`runner/app.py:6270`, `FAIL_CLOSED_PHASES` excludes them). A cost/PII
   LLM-phase policy is silently skipped during a transient server outage.
2. **Native REQUEST gate is dedup'd by a heuristic, not an id** — a web prompt in flight is detected by
   `pending_inputs.snapshot_for` (`sessions.py:16065`); if that signal desyncs (see memory:
   native-firstmsg-fifo-desync), a web prompt could be re-gated (double-prompt) or a TUI prompt skipped.
3. **In-memory only** — `pending_elicitations` index and `_harness_elicitation_registry` are per-process;
   a multi-replica deploy each sees its own slice (`pending_elicitations.py:31`). The pre-resolved
   tombstone (`sessions.py:4006`) only patches the *same-replica* severed-long-poll gap.
4. **Hook token expiry** — historically the one-shot hook token lapsed (~1h) and the gate failed CLOSED
   on every tool call (memory: native-hook-token-expiry-failclosed). Now mitigated: `policy_hook_reauth`
   (`native_policy_hook.py:133`) re-mints on 401/302. But `pi_native` (Node hook) is the remaining gap
   (memory: native-hook-reauth-landscape) — out of scope here, flagged for completeness.
5. **`monotonic` without `values` asserts at runtime** (`engine.py:1224`) — a parser regression would
   500 the eval rather than degrade. Deliberate fail-loud.
6. **nessie shell classifier is a heuristic, not a boundary** — `_shell_statements` (`nessie/policies.py:134`)
   does not model subshells / `eval` / command substitution; a determined caller evades it. Sandboxing
   (OmniBox) is the real boundary; blast_radius is accidental-damage protection only.

#### Corrections to CUJ-ANALYSIS §2.D

1. **Drifted line anchors** (verify-and-fix): resolve route is `sessions.py:18014` (CUJ says `:17611` — that
   is now an unrelated `_proxy_fs_response`); `_evaluate_tool_call_policy` is `sessions.py:10556` (CUJ says
   `:10384`); the evaluate endpoint is `sessions.py:15964`. `session_policies.py:148` and
   `default_policies.py:129` and `runner/policy.py` are **correct**.
2. **codex-native hook row is mislabeled.** CUJ's table puts codex-native on a single
   `codex-elicitation-request` hook. In code there are **two distinct paths**: the **policy** gate uses the
   *same shared* `PreToolUse/PostToolUse/UserPromptSubmit` hook as claude (`codex_native_hook.py:43` →
   `/policies/evaluate`, long-poll); the `codex-elicitation-request` endpoint
   (`codex_native_forwarder.py:3146`) is for codex's **own** permission prompt (`item/tool/requestUserInput`),
   not the policy ASK. The "verdict via long-poll HTTP" conclusion holds for both.
3. **"REQUEST/RESULT/LLM = fail-OPEN" is too coarse on two counts.** (a) **REQUEST fails CLOSED**, not open —
   it is in `FAIL_CLOSED_PHASES=("PHASE_TOOL_CALL","PHASE_REQUEST")` (`policies/types.py:61`;
   `native_policy_hook.py:426` blocks the prompt on an unreachable server). (b) The fail rule for a *raising
   policy* is by declared `action:` list, not phase — classifier-only `[allow]` fails ALLOW, `[ask]` fails
   ASK (`engine.py:1038`). The CUJ row conflates the transport-unreachable rule with the per-policy rule.
   (Minor: LLM_REQUEST/LLM_RESPONSE are correctly fail-OPEN.)

---

### Tools / MCP / Sandbox (OmniBox)

Scope: tool registry + `sys_*` surface, MCP routing (Omnigent-MCP vs custom vs native relay
vs serve-mcp), shells/terminals/cwd, the OS sandbox (filesystem/egress/credential), timers,
system resources. Harnesses in scope: claude-sdk, claude-native, codex, codex-native, Polly.

All `path:line` below were opened and confirmed against the `master-arch-docs` worktree
(main + #1617). Anything I could not confirm is tagged `(unverified)`.

---

#### 1. Role & boundaries

This component owns **what tools exist, what schema the LLM sees, and where a tool call
physically runs** — but it does **not** own policy enforcement (that's the server's
`policies/evaluate` + the `/mcp` gate), nor the transcript, nor the harness loop.

Two layers, by deployment mode:

- **Registry / schema layer** — `ToolManager` (`omnigent/tools/manager.py:97`) builds the
  per-spec tool set: skills, builtins, `sys_*`, sub-agent tools, `sys_os_*`, `sys_terminal_*`,
  local-python, client tools. **MCP tools are NOT registered here** (`manager.py:4,48,102`:
  "MCP lifecycle lives on the runner"). This `ToolManager` is the legacy/in-process registry;
  on the live runner-based stack the schemas reach the harness another way (see §4/§6).
- **Dispatch layer** — the runner's `POST /v1/sessions/{id}/mcp/execute`
  (`omnigent/runner/app.py:17829`) is the single uniform execution endpoint for **every**
  tool category: namespaced `server__tool` → `RunnerMcpManager`; bare `sys_*` → `execute_tool`.

The **OS sandbox ("OmniBox")** is a *policy applied to a helper subprocess*, not a UI and not
a separate process type. It is three layers (`omnigent/inner/sandbox.py`,
`omnigent/inner/egress/`, `omnigent/inner/credential_proxy.py`): filesystem isolation +
default-deny egress + L7 credential injection. It is owned here; the egress proxy and
credential resolution run in the **trusted parent** (runner side), never inside the sandbox.

What this component does NOT own: policy decisions (server `policies/evaluate`), the WS tunnel
(server↔runner transport), the harness turn loop, transcript persistence.

---

#### 2. Key files & entrypoints (verified)

| Concern | path:line |
|---|---|
| Tool registry | `omnigent/tools/manager.py:97` (`ToolManager`), `:373` sub-agent gate, `:525` os_env, `:563` terminal, `:608` local, `:873` `call_tool` |
| Runner-local dispatch | `omnigent/runner/tool_dispatch.py` (`execute_tool`); MRTR const `MCP_PROXY_FORWARD_TIMEOUT_S` (`:13410` ref) |
| Runner `/mcp/execute` | `omnigent/runner/app.py:17829` (`mcp_execute`); `__`-split at `:17954`, bare at `:18044` |
| Custom-MCP pool (direct) | `omnigent/runner/mcp_manager.py:150` (`RunnerMcpManager`), `:74` `compute_spec_hash`, `:98` `_mcp_tool_schema` (`{srv}__{tool}`), `:45` LRU cap 8, `:182` inline elicitation |
| Custom-MCP pool (proxy) | `omnigent/runner/proxy_mcp_manager.py:42` (`ProxyMcpManager`) → server `/mcp` |
| Server `/mcp` MCP server | `omnigent/server/routes/sessions.py:13056` (`tools/list`), `:13135` (`tools/call`), `:12796` `_mcp_tool_result`, `:12989` `_mcp_input_required_response` |
| Server MCP pool (DEAD for proxy) | `omnigent/server/mcp_pool.py:105` (`ServerMcpPool`); unused in `/mcp` handler — `sessions.py:13605` `# ARG001 retained for API compat` |
| Custom-MCP spec | `omnigent/spec/types.py:845` (`MCPServerConfig`: `transport: http|stdio`, `:933` `tools` allowlist) |
| Native bridge / serve-mcp | `omnigent/claude_native_bridge.py:3116` `_serve_mcp`, `:3522` `tools/call`, `:3586` `_call_mcp_tool` (relay-vs-local switch at `:3609`), `:3658` `_call_relay_tool`, `:3041` `start_tool_relay` |
| SDK Omnigent-MCP | `omnigent/inner/claude_sdk_executor.py:603` `_build_mcp_tools`, `:1864` `create_sdk_mcp_server(name="omnigent")`, `:668` prompt augment (`mcp__omnigent__*`) |
| Sandbox policy | `omnigent/inner/sandbox.py:49` (`SandboxPolicy`), `:371` `resolve_sandbox`, `:598` `run_launcher`; backends `bwrap_sandbox.py`, `seatbelt_sandbox.py`, `windows_jobobject_sandbox.py` |
| OS env | `omnigent/inner/os_env.py:263` (`OSEnvironment` ABC), `:778` `CallerProcessOSEnvironment`, `:883` `create_os_environment`, `:639` `_start_egress_proxy_locked` |
| Egress proxy | `omnigent/inner/egress/proxy.py:133` (`EgressProxy`), `egress/rules.py:185` `check_request` (default-deny), `egress/controller.py:126` `start_egress_proxy`, `:260` `apply_egress_env` |
| Credential proxy | `omnigent/inner/credential_proxy.py:103` `prepare_credential_proxy_runtime`, `:52` `CredentialRewriteRule`, `:46` `oa_cred_` prefix |
| `sys_os_*` tools | `omnigent/tools/builtins/os_env.py:199` read, `:247` write, `:289` edit, `:334` shell, `:378` `build_os_env_tools` |
| `sys_terminal_*` + cwd | `omnigent/tools/builtins/sys_terminal.py:752` `_resolve_cwd` (§4.6 precedence) |
| Terminal runtime | `omnigent/inner/terminal.py:718` `TerminalInstance` (tmux, `session_key`); registry `omnigent/terminals/registry.py`, getter `omnigent/runtime/__init__.py:get_terminal_registry` |
| Timers | `omnigent/tools/builtins/timer.py:49` set (**raises NotImplementedError `:220`**), `:226` cancel (always `not_found`) |

---

#### 3. Internal model

##### Tool registry (`ToolManager`)
Plain `dict[str, Tool]` built once at `__init__` (`manager.py:139-184`). Registration order &
gating:
- always: skills (`load_skill`), discovery sub-agent reads (`sys_session_list/get_history/get_info`),
  `sys_agent_get/download/list`, `list_comments`/`update_comment`, `sys_cancel_task`,
  `sys_add_policy`/`sys_policy_registry`.
- gated: builtins (`tools.builtins`), `sys_os_*` iff `os_env` declared (`:525`),
  `sys_terminal_*` iff `terminals:` non-empty (`:563`), spawn tools (`sys_session_send/close`,
  `sys_list_models`) iff `tools.agents` or `spawn:true` (`:447`); `sys_session_create` iff
  `spawn:true` (`:468`); `sys_session_share` iff `agent_session_sharing != none` (`:441`);
  async (`sys_call_async/read_inbox/cancel_async`) iff `async_enabled` (default True, `:225`);
  timers iff `timers:true` (default False, `:257`).
- Tool taxonomy by dispatch: **server-side callable** (LocalCallableTool), **client-side**
  (`ClientSideTool` → `action_required`, `:942`), **UC-function** (`_UCFunctionSchemaTool`,
  schema-only, runner dispatches via SQL Stmt API, `:53`), **schema-only** (MCP — not here).
- `get_tool_schemas` (`:835`) and `get_client_tool_schemas` (`:896`) are per-tool fail-soft:
  one tool's bad `get_schema()` is skipped, not fatal (#378).

##### Custom-MCP pool — two interchangeable managers (same interface)
- `RunnerMcpManager` (`mcp_manager.py:150`): per-runner pool, `dict[spec_hash → _SpecEntry]`,
  LRU list cap **8** (`:45,:476`). `compute_spec_hash` (`:74`) hashes `{name,transport,url,
  command,args,env,tools}` + stdio `cwd`. Connect-all is concurrent (`:506`), per-server
  failures **recorded** (`server.error`) not raised, so one dead MCP doesn't sink the rest.
  Namespacing `{server}__{bare}` (`:141`); route resolution strips prefix (`:387`). Used in
  **no-AP/test mode** (direct stdio/SSE connections; runner enforces policy locally).
- `ProxyMcpManager` (`proxy_mcp_manager.py:42`): drop-in substitute used in **AP mode** —
  every call POSTs JSON-RPC to the server `/mcp` so policy is enforced centrally. `prewarm`/
  `shutdown` are no-ops (`:319,:326`). The runner instantiates `ProxyMcpManager(conv,
  server_client)` per-turn (`runner/app.py:13653,:14056,:14474,:13108`) — **this is the live
  default**.

##### `ServerMcpPool` (`server/mcp_pool.py:105`) — vestigial
Keyed by `agent_id`, warm-on-demand, LRU. **NOT used by the `/mcp` route** — the handler
delegates execution to the runner (`sessions.py:13605` param is `# ARG001 retained for API
compat`, docstring `:13638` "Unused"). See §7.

##### OS sandbox model (`SandboxPolicy`, `sandbox.py:49`)
Resolved policy serialized parent→helper. Fields: `read_roots`/`write_roots`/`write_files`
(filesystem view), `allow_network`, `egress_relay_port`/`egress_socket_path`
(default-deny relay), `deny_unix_socket_paths` (block reach-back to control sockets),
`cwd_allow_hidden` (dotfile masking), `spawn_env_allowlist`/`env_passthrough` (env pruning).
**`credential_proxy` is deliberately NOT serialized** (`:151-157`) — only synthetic
placeholders cross into the helper; real secrets never touch the policy that logs/dumps.
Backends: `linux_bwrap`, `darwin_seatbelt` (both **spawn-time**: wrap argv with launcher),
`none`. `_SPAWN_WRAP_BACKENDS` (`:41`).

##### OSEnvironment
ABC `os_env.py:263`; the **only** concrete impl is `CallerProcessOSEnvironment` (`:778`).
`create_os_environment` (`:883`) raises `NotImplementedError` for any `spec.type !=
"caller_process"` (`:887`). `fork` and `sandbox` are **attributes/modes** of that one env, not
distinct env types: `fork=true` copy-trees cwd into `omnigent-fork-*/root` (`:892-896`);
`sandbox` is the resolved `SandboxPolicy` applied to each `shell()` helper subprocess. (Correction in §8.)

##### Terminal
`TerminalInstance` (`terminal.py:718`) = one command in its own private tmux server
(isolated socket). Keyed `(conversation_id, terminal_name, session_key)`. Lives in the
AP-process `TerminalRegistry` so panes survive across turns within a conversation
(`manager.py:583`).

---

#### 4. Inter-component channels

```
                        ┌──────────────────────────── server (omni-server) ────────────────────────────┐
 LLM (in harness)       │  POST /v1/sessions/{id}/mcp   ←── JSON-RPC 2.0 MCP server (sys_* + custom MCP) │
   │ calls tool         │     ├─ TOOL_CALL policy (DENY / ASK→MRTR InputRequired) sessions.py:13135      │
   ▼                    │     ├─ sys_advise_models intercept (server-local) :13392                       │
 harness tool_executor  │     └─ forward → runner /mcp/execute  (over WS tunnel)  :13412                 │
   │                    │  POST /v1/sessions/{id}/mcp/execute  →── dispatched on RUNNER                  │
   ▼ (per harness)      └──────────────────────────────────────────────────────────────────────────────┘
 ┌── claude-sdk: in-proc SDK MCP server "omnigent" (create_sdk_mcp_server) → handler → tool_executor
 ├── claude-native: serve-mcp stdio child → tool_relay.json switch → localhost HTTP relay → tool_executor
 └── codex/codex-native: (unverified detail; same /mcp proxy contract via tool_executor)
                         │
                         ▼
            runner /mcp/execute (app.py:17829)
              ├─ "server__tool" → RunnerMcpManager → stdio/SSE subprocess  (custom MCP)
              └─ bare "sys_*"   → execute_tool → terminal registry / inbox / OS env / ProxyMcpManager
```

| Edge | Peer | Transport | Op / message | Durable? |
|---|---|---|---|---|
| runner → server | server | **WS-tunnel** (httpx over `/v1/runners/{id}/tunnel`) | `POST /v1/sessions/{id}/mcp` JSON-RPC `tools/list`,`tools/call` | streaming RPC |
| server → runner | runner | **WS-tunnel** | `POST /v1/sessions/{id}/mcp/execute` `{method,params}` | streaming RPC |
| server → client | TUI/Web | **SSE** | `response.elicitation_request` / `mcp_elicitation` (ASK) | streaming |
| serve-mcp ↔ Claude | Claude Code child | **stdio JSON-RPC** | `initialize`/`tools/list`/`tools/call`/`notifications/tools/list_changed` | streaming |
| relay (bridge→harness) | harness turn | **localhost HTTP** `POST /tool` (bearer) | one tool dispatch | request/response |
| serve-mcp control | harness | **localhost HTTP** `POST /tools-changed` (bearer) | emit `tools/list_changed` | one-shot |
| sandbox helper → parent | egress proxy | **UDS** (`egress_socket_path`) via in-ns relay on `egress_relay_port` | all HTTP(S) CONNECT | per-request |

**Trace evidence (`conv_fc47380…`, subagent/Polly):**
- `omni-runner → omni-server [POST /v1/sessions/{id}/mcp] x3` and
  `omni-server → omni-runner [POST /v1/sessions/{id}/mcp/execute] x3` — the runner's
  `ProxyMcpManager` calling the server `/mcp`, server forwarding back to runner `/mcp/execute`.
  These x3 are `sys_session_send`, two `sys_read_inbox` (captured payloads, summary lines 56-69).
- Every `/mcp` `tools/call` is preceded by a `policies/evaluate` (`policy.evaluate x14`) — the
  TOOL_CALL/TOOL_RESULT gate.
- **Negative evidence:** the `sdk-tools` (`conv_32db…`) and `native-tools` (`conv_94e6…`) convs
  have **zero** `/mcp*` edges — because those test agents declared no `os_env`/`terminals`, so
  `sys_os_shell` was never registered ("No shell tool available here", `conv_32db…` payload
  line 49, `tools_count:15`). The simple convs therefore are NOT shell-dispatch evidence;
  only the subagent conv exercises the `/mcp` proxy.

---

#### 5. CUJ behaviors

##### A tool call, per harness (the routing answer)
Both in-scope claude harnesses funnel **every** Omnigent `sys_*` call through the harness's
`tool_executor` → runner session dispatch → (`ProxyMcpManager`) server `/mcp` policy gate →
runner `/mcp/execute`. They differ only in *how the schema is advertised to the model*:

- **claude-sdk**: harness registers an in-process SDK MCP server `name="omnigent"`
  (`claude_sdk_executor.py:1864`); model sees tools as `mcp__omnigent__<bare>`; the prompt is
  augmented to teach the renamed form (`:668`). Native `Bash/Read/Edit/Write` are deliberately
  NOT allowed — OS ops route through `sys_os_*` for runner visibility/timeouts (`:1874`).
  Handler (`:627`) → `tool_executor` → dispatch → `action_required` → runner re-dispatch.
- **claude-native**: Claude Code spawns the `serve-mcp` stdio child (`claude_native_bridge.py
  :3116`). On `tools/call` (`:3586`), `_call_mcp_tool` checks `tool_relay.json` (`:3609`):
  - **turn active** → relay tool overrides local; forward over localhost HTTP `POST /tool`
    (`:3658`) → harness `tool_executor` → same server `/mcp` path (so the call appears in the
    Omnigent event stream + is policy-gated).
  - **no active turn** → run `sys_os_*` **directly in the bridge process** against the
    workspace (`:3622`), with NO server policy gate. ⚠️ out-of-turn `sys_os_*` are
    policy-ungated by construction (the relay isn't published).
- **codex / codex-native**: same `/mcp` proxy contract via `tool_executor` (the dispatch sites
  in `runner/app.py` are harness-agnostic). Codex-specific schema-advertisement mechanism
  `(unverified)` — not traced.
- **Polly**: inherits its underlying harness's path verbatim; the subagent trace IS Polly-style
  (orchestrator delegating to a `worker` sub-agent via `sys_session_send`).

##### Custom user MCP (`tools.mcp` / `tools/mcp/<name>.yaml`)
Spec `MCPServerConfig` (`types.py:845`): `transport: http|stdio` (default `http`). `http` →
SSE client (`url`,`headers`,`databricks_profile` OAuth). `stdio` → local subprocess
(`command`,`args`,`env`) — **runs unsandboxed** (`types.py:860`; the old `srt` wrap was removed
because its default network-deny silently hung MCPs). Per-server `tools:` allowlist filters at
registration (`:933`). Pooled by `RunnerMcpManager` keyed on `compute_spec_hash`. In AP mode
the call still flows runner→server `/mcp`→runner `/mcp/execute`→`RunnerMcpManager`→subprocess.

##### ASK / elicitation round-trip (MRTR)
On TOOL_CALL=ASK the server returns an MCP `InputRequiredResult` (`_mcp_input_required_response`,
`sessions.py:12989`) + emits an SSE `response.elicitation_request`. The runner's
`ProxyMcpManager.call_tool` parks on `pending_approvals.wait_for_user_approval` and retries once
with `inputResponses` (`proxy_mcp_manager.py:257-300`). Retry id MUST differ (`id:2`, `:289`).
**Server re-evaluates the policy on retry** (`sessions.py:13239`) — a forged/unsigned
`requestState` can't bypass a DENY (fail-closed). External-MCP elicitations bubble the same way
(`sessions.py:13434` `input_required` → SSE → retry on runner). So **elicitation responses get
back via the approval Future + SSE-driven UI, NOT keystrokes** (keystrokes are only the
native *web-input* path via tmux send-keys, a separate channel).

##### Shells exposed to agents
- `sys_os_shell` (`os_env.py:334`): one-shot `command` → `OSEnvironment.shell()` (async,
  `:363`); returns `{stdout,stderr,exit_code}`. Gated on `os_env:` in spec.
- `sys_terminal_*` (`sys_terminal.py`): persistent **tmux panes** for interactive REPLs; gated
  on `terminals:` block. `launch/send/read/list/close`. This is also how native harnesses get
  their own TUI pane (the runner auto-creates a `claude/main` terminal).

##### OmniBox (the 3 layers) for one sandboxed shell
1. **Filesystem isolation**: bwrap/seatbelt hermetic root; `read_roots` → `--ro-bind-try`,
   `write_roots`/cwd → writable, dotfiles tmpfs-masked unless in `cwd_allow_hidden`.
2. **Default-deny egress** (`egress/rules.py:185`): in-namespace relay on `egress_relay_port`
   forwards over UDS to the parent's `EgressProxy` (`egress/proxy.py:133`), an L7 CONNECT MITM.
   Requests are matched against `METHODS host/path` allow-rules; **no match → 403**. Host
   grammar locked to `[A-Za-z0-9.-]` to kill NUL/CRLF/percent smuggling (`rules.py:49`).
   `apply_egress_env` injects `HTTP_PROXY`/`HTTPS_PROXY`/CA (`controller.py:260`).
3. **Credential injection** (`credential_proxy.py`): real secrets stay in the parent. Default
   **swap-on-access** — sandbox sends no `Authorization`, proxy attaches the real cred for the
   bound host. Opt-in `inject_env` mints an `oa_cred_<token>` placeholder (`:141`) for clients
   that demand a local cred (e.g. `gh`); proxy swaps it and **403s a placeholder replayed to a
   different host** (cross-host leak guard, `:60-64`). ⚠️ #1542 trust-boundary concern: the MITM
   sees plaintext of every sandbox request (it must, to inject) — see §7.

---

#### 6. Answers to the doc questions (terse, code-anchored)

- **Omnigent MCP (sys_* surface), how exposed + dispatched?** The server runs an MCP JSON-RPC
  endpoint at `POST /v1/sessions/{id}/mcp` (`sessions.py:13056/13135`). Harnesses advertise the
  `sys_*` schemas to their model (SDK: in-proc `mcp__omnigent__*`; native: serve-mcp stdio).
  When called, the harness's `tool_executor` routes through the runner → `ProxyMcpManager` →
  server `/mcp` (policy: TOOL_CALL+TOOL_RESULT) → runner `/mcp/execute` (`app.py:17829`,
  bare-name branch → `execute_tool`). So the **server is the policy gate, the runner is the
  executor**, for both `sys_*` and custom MCP.
- **Custom MCP (yaml `tools.mcp`)** — `MCPServerConfig` `http`(SSE)/`stdio`; pooled by
  `RunnerMcpManager` (LRU 8, hash-keyed); namespaced `{server}__{tool}`; per-server `tools`
  allowlist; stdio unsandboxed. AP mode routes through the same server `/mcp` gate; no-AP mode
  connects directly and gates locally (`RunnerToolPolicyGate`).
- **Native in-turn relay vs out-of-turn serve-mcp** — serve-mcp (`_serve_mcp`,
  `claude_native_bridge.py:3116`) is one long-lived stdio MCP server Claude Code spawns.
  `tools/call` switches on `tool_relay.json`: **in-turn** → localhost-HTTP relay to the harness
  (`_call_relay_tool`, `:3658`) → server `/mcp` (gated, visible in event stream); **out-of-turn**
  → runs `sys_os_*` locally in the bridge (`:3622`, ungated). `tools/list` merges local + relay
  schemas, relay overriding (`:3544`).
- **Who routes a tool call where, per harness** — see §5.A. Universal: model → harness
  `tool_executor` → runner dispatch → (AP) server `/mcp` gate → runner `/mcp/execute`. SDK and
  native differ only in schema advertisement.
- **Shells created + cwd resolution (`_resolve_cwd`)** — `sys_terminal.py:752`, §4.6 first-match:
  (1) LLM `cwd_override` (only if `allow_cwd_override:true`, else rejected `:390`) →
  (2) terminal's own `os_env.cwd` if meaningful → (3) `spec.os_env.cwd` if meaningful →
  (4) `ctx.workspace` → (5) `None` (host cwd). `_has_meaningful_cwd` rejects `None/""/"."/"./"`.
- **Shells exposed: `sys_os_shell` vs `sys_terminal_*`** — `sys_os_shell` = one-shot
  command+capture (gated on `os_env`); `sys_terminal_*` = persistent tmux panes for interactive
  programs (gated on `terminals`). Different tools, different lifecycles.
- **OmniBox 3 layers** — filesystem isolation (bwrap/seatbelt mounts), default-deny egress proxy
  (`METHODS host/path` allowlist, 403 otherwise), credential injection (swap-on-access +
  optional `oa_cred_` placeholder, cross-host 403 guard). Real secrets parent-only; not
  serialized into the policy.
- **Timers (`sys_timer_set/cancel`)** — gated on `timers:true`. **On the live sessions-native
  (runner) stack they DON'T work**: `_spawn_timer_workflow` raises `NotImplementedError`
  (`timer.py:220`) and `sys_timer_cancel` always returns `not_found` (`:303`). The design
  (firings ride the `async_work_complete` drain with `kind="timer"`) exists; the runner-side
  implementation does not.
- **System resources generally** — per-conversation, AP-process-resident registries:
  `TerminalRegistry` (tmux panes, keyed `(conv,name,session_key)`), session inbox
  (`_session_inboxes`), async tasks (`_session_async_tasks`), `SessionResourceRegistry` (the
  pre-resolved primary `OSEnvironment`, shared into `ToolManager` so `sys_os_*` reuse one env,
  `manager.py:133`). These survive across turns within a conversation; the runner threads them
  into `execute_tool` (`app.py:18070-18088`).

---

#### 7. Reliability gaps / sharp edges (confirmed in code)

1. **Out-of-turn native `sys_os_*` are policy-ungated.** When no turn is active the serve-mcp
   bridge runs `sys_os_read/write/edit/shell` directly in-process (`:3622`) with no server
   `/mcp` gate. A user typing in the Claude pane between web turns has full ungated workspace
   FS+shell. By design (workspace access), but it's a real asymmetry vs in-turn calls.
2. **Timers are silently dead on the runner stack** (`timer.py:220`). An agent with
   `timers:true` gets the tool in its schema, calls `sys_timer_set`, and hits
   `NotImplementedError` (surfaced as a tool error). Schema advertises a capability that doesn't
   exist.
3. **`ServerMcpPool` is dead code on the proxy path** (`sessions.py:13605` ARG001). The
   server-owned MCP connection pool exists, has LRU/warm-on-demand machinery, but the live
   `/mcp` handler never uses it — all custom-MCP execution is runner-side. Maintenance hazard:
   two pools, one live (`RunnerMcpManager`), one vestigial.
4. **`RunnerMcpManager` LRU cap = 8 specs** (`mcp_manager.py:45`). 9+ distinct MCP specs
   active on one runner evicts the LRU victim's live stdio connections mid-flight (close runs as
   a fire-and-forget task, `:492`). A busy multi-agent runner can thrash MCP subprocesses.
   `# ponytail: fixed cap, fine until a runner hosts >8 distinct MCP specs.`
5. **Credential MITM trust boundary (#1542).** The egress proxy terminates TLS to inspect and
   inject (`egress/proxy.py` CONNECT MITM); it necessarily sees plaintext of every sandbox
   request. The CA private key + real secrets live in the parent. If the parent (runner) is
   compromised, all injected creds + all sandbox traffic are exposed — the sandbox boundary
   protects the host from the agent, not the agent's creds from a compromised runner.
6. **Spec-hash misses don't dispatch.** `RunnerMcpManager.call_tool` requires the spec's
   `compute_spec_hash` to match a pooled entry; a cwd change (stdio `cwd` is in the hash, `:75`)
   between schema-listing and dispatch yields "runner has no live MCP serving tool"
   (`:376`). Subtle for stdio MCPs whose cwd derives from terminal cwd resolution.
7. **Two custom-MCP namespacing owners.** `RunnerMcpManager` applies `{server}__{tool}` (`:141`);
   `ServerMcpPool` explicitly does NOT (`mcp_pool.py` docstring) and pushes it onto callers.
   Since the proxy path is runner-side this is currently consistent, but any future re-use of
   `ServerMcpPool` would double- or un-namespace.

---

#### 8. Corrections to CUJ-ANALYSIS §2.C

1. **"OSEnvironment types: caller_process / fork / sandbox" is wrong.** There is exactly one
   concrete env, `CallerProcessOSEnvironment`; `create_os_environment` raises
   `NotImplementedError` for any other `type` (`os_env.py:887`). `fork` and `sandbox` are
   **attributes/modes** of that single env (fork = copy-tree cwd; sandbox = a `SandboxPolicy`
   applied to each helper subprocess), not separate environment classes. Treat as one env with
   two orthogonal knobs.
2. **The server does NOT execute MCP tools / does not use `ServerMcpPool` for the `/mcp`
   proxy.** Any claim that the server holds the live MCP connections for the proxy path is
   stale — execution is delegated to the runner's `/mcp/execute` (`RunnerMcpManager`);
   `ServerMcpPool` is unused (`sessions.py:13605` `# ARG001 retained for API compat`,
   docstring `:13638` "Unused"). The server is the *policy gate*, the runner is the *executor*.
3. **`sys_timer_*` are not functional on the current (runner-based) stack.** Any anchor implying
   working timers is wrong for main: `timer.py:220` raises `NotImplementedError`; cancel always
   returns `not_found`. (The `async_work_complete`/`kind="timer"` drain design is real but
   unimplemented runner-side.) Tag timer behavior `(unverified→confirmed-broken)`.

*Note:* `manager.py`'s `ToolManager` docstrings ("MCP lifecycle lives on the runner",
`designs/RUNNER_MCP.md`, `designs/SERVER_HARNESS_CONTRACT.md`) are accurate as pointers but the
design docs themselves were not re-verified — treat their prose as `(unverified)` per the
ground-truth rule; the code anchors above are confirmed.

---

### Component: Agents / Subagents / Routing / Inbox

Scope: custom-agent storage + caching, sub-agent spawning + info propagation + depth,
intelligent routing, async/inbox, resume dispatch. Verified against the running code in
`/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs` + trace
`conv_fc47380ccbff481abf452a446ec4e40d` (the subagent-spawn / "BANANA" Polly run).

---

#### 1. Role & boundaries

**Owns:**
- Sub-agent spawn surface (`sys_session_send` / `sys_session_create` and the read/observe tools
  `sys_session_list/get_history/get_info/close/share`) — schemas + in-process impls in
  `omnigent/tools/builtins/spawn.py`; **the actual minting** of a child Conversation on the
  **runner** in `omnigent/runner/tool_dispatch.py::_execute_subagent_tool`.
- Child↔parent result return via the per-session **inbox** (`asyncio.Queue` per session) +
  `async_work_complete`-style drain (`sys_read_inbox`); async tool dispatch (`sys_call_async`).
- Custom-agent storage tiers (ArtifactStore → `Agent` DB row → server `AgentCache`) and the
  separate runner-side bundle disk cache.
- Server-side intelligent model routing (`omnigent/server/smart_routing.py::route_turn`).
- CLI `omnigent resume` dispatch (`omnigent/resume_dispatch.py`).
- The AgentTool/SelfAgentTool spec **types** (`omnigent/inner/tools.py`) + their materialization
  into nested `AgentSpec` (`omnigent/spec/omnigent.py`).

**Does NOT own (cross-ref):** the executor turn-loop & SDK event translation (Executor section);
**fork** mechanics (`POST /v1/sessions/{id}/fork` — Server-routes section; fork is a sibling of
spawn but a different flow); runner↔server WS-tunnel transport (Runner/transport section); MCP
routing internals (MCP section); policy evaluation engine (Policy section); claude-native
subagent **bridge file-watch** beyond noting the entrypoint (Native-harness section).

---

#### 2. Key files & entrypoints (verified path:line)

| What | Anchor |
|---|---|
| AgentTool / SelfAgentTool **dataclasses** (spec types only — no dispatch) | `omnigent/inner/tools.py:266` (AgentTool), `:298` (SelfAgentTool); fields `pass_history`/`pass_histories`/`max_sessions` `:289-295` |
| `sys_session_send` schema + in-proc tools | `omnigent/tools/builtins/spawn.py:56` (SysSessionSendTool), `:766` (SysSessionCreateTool), `:439` (List), `:1282` (GetHistory), `:1425` (Close) |
| **Spawn dispatch** (mint child Conversation) | `omnigent/runner/tool_dispatch.py:1146` `_execute_subagent_tool`; child create `POST /v1/sessions` `:1435`; message POST `:1538`; handle return `:1567`; by-session-id mode `:1586` |
| `sys_session_create` dispatch | `omnigent/runner/tool_dispatch.py:1828` `_execute_session_create`; body builder `:1730` (`parent_session_id` hard-forced `:1755`) |
| Inbox drain (`sys_read_inbox`) | `omnigent/runner/tool_dispatch.py:5476` `_drain_inbox`; delayed-policy eval `:5409`; consume-once via `get_nowait` `:5500` |
| Completion push into parent inbox | `omnigent/runner/app.py:7248` `_deliver_subagent_completion` (`inbox.put_nowait` `:7279`); wake notice `:7456` `_format_subagent_wake_notice` |
| Child→parent fan-out registry | `omnigent/runner/app.py:7041` `register_subagent_work`; `_session_inboxes_ref` `:7692`; queue create `:8888` |
| Child turn spec swap (own-subagents init) | `omnigent/runner/app.py:8717-8728` (`_find_spec_by_name(spec, sub_agent_name)`) |
| Sub-agent spec resolution + web-researcher fallback | `omnigent/runtime/workflow.py:2546` `_find_spec_by_name`; pure search `:2639` `_search_sub_agent_tree` |
| SelfAgentTool clone + parse-time recursion guard | `omnigent/spec/omnigent.py:1252` `_build_self_clone_subspec`; strip-self `:1307-1310` |
| Server agent cache (2-tier) | `omnigent/runtime/agent_cache.py:16` AgentCache; `load` `:51`; warm-swap `replace` `:102`; `evict` `:161` |
| Runner bundle disk cache (separate) | `omnigent/server/bundles.py:561` `_agent_cache_dest`; download+parse `:645-656` |
| `Agent` DB entity (3rd tier) | `omnigent/entities/agent.py:11` (`bundle_location` `:36`, `version` `:37`, `session_id` `:40`) |
| Smart routing | `omnigent/server/smart_routing.py:215` `route_turn`; `MODEL_LISTS` `:25`; `_HARNESS_FAMILY` `:49`; `infer_models` `:62`; LLM judge `:155` |
| route_turn call site (first-message gate) | `omnigent/server/routes/sessions.py:8650-8684` |
| Resume dispatch | `omnigent/resume_dispatch.py:39` `run_resume`; wrapper→harness `:201` `_dispatch_wrapper` |
| `sys_cancel_task` (no-op) | `omnigent/tools/builtins/async_inbox.py:97-126` |
| Spawn-depth constant (display-only) | `omnigent/repl/_repl.py:201` `_MAX_SUBAGENT_TREE_DEPTH=3`; used `:6271` (`_refresh_subagent_tree` sidebar poll) |

---

#### 3. Internal model

**Sub-agent identity & tree.** A named sub-agent is a normal Omnigent Conversation row with:
- `title = "<agent>:<title>"` (e.g. `"worker:banana"`), the composite the runner scans for
  spawn-vs-continue (`tool_dispatch.py:1404`, `_find_existing_child_session:837`).
- `parent_conversation_id` = the **immediate** caller (Phase-4; nested sub-agents point at their
  spawning sub-agent, not the root — `spawn.py:407` `_resolve_parent_conversation_id`).
- `root_conversation_id` = the spawn-tree root (NOT NULL post-migration `d8e2f3b4c910`). **All
  tree-scoping (peek/close) is enforced by `root_conversation_id` match**, not parentage
  (`spawn.py:1197`).
- `sub_agent_name` column (migration `f1a2b3c4d5e6`) — the dispatched sub-agent's name; the runner
  uses it to resolve the child's spec from the parent bundle.

**Close = tombstone, not delete.** `sys_session_close` rewrites title to
`"<agent>:<title>:closed:<conv_id>"` + sets label `omnigent.closed=true`
(`spawn.py:1530-1535`). Items survive; the `(agent,title)` lookup no longer matches so a re-send
spawns a fresh child.

**Per-session inbox.** `_session_inboxes_ref: dict[conv_id, asyncio.Queue]` on the runner
(`app.py:7692`). A queue is created lazily per session (`app.py:8888`). Sub-agent completions and
`sys_call_async` results are `put_nowait`'d here; drained consume-once by `sys_read_inbox` or by
the loop's between-iteration auto-collect.

**Storage = 3 tiers** (see §6 storage answer). `Agent` row carries
`bundle_location` (content-addressed `<agent_id>/<sha256>`), monotonic `version`, and
`session_id` (None = operator/template agent; non-null = session-scoped tenant agent — gates
`expand_env`).

**AgentCache tiers** (`agent_cache.py`): Tier-1 in-memory `_specs: dict[agent_id, AgentSpec]`,
Tier-2 disk `<cache_dir>/<agent_id>/`, source-of-truth = ArtifactStore tarball. **No TTL** — only
explicit `evict()` (on delete) and `replace()` (warm-swap on update). The runner keeps a
**separate** disk cache keyed by `(agent_id, version)` (`bundles.py:561`).

---

#### 4. Inter-component channels (in/out edges)

All durable state is the conversation/agent rows; SSE/queue events are transient.

```
 ORCHESTRATOR turn (claude-sdk)        RUNNER (tool_dispatch)            SERVER (REST)
 ─ LLM emits sys_session_send ──────▶ _execute_subagent_tool
   (action_required SSE)               │ POST /v1/sessions ───────────▶ create child conv row
                                       │   {agent_id=PARENT, parent_   (durable; SessionResponse)
                                       │    session_id, title=a:t,
                                       │    sub_agent_name}
                                       │ register_child_session +
                                       │   register_subagent_work (local)
                                       │ publish session.created ─────▶ relay → parent SSE stream
                                       │ POST /v1/sessions/{child}/      (server forwards to runner,
                                       │   events  (user message) ────▶  starts child turn)
                                       │ return handle{status:launching}
   LLM gets handle ◀───────────────────┘
   ... parent turn ends or yields ...
                          child turn completes on runner →
                          _deliver_subagent_completion: inbox.put_nowait({type:sub_agent,...})
                          + wake POST  ─────────────────▶ parent /events ▶ relaunch parent turn
   LLM calls sys_read_inbox ─────────▶ _drain_inbox: get_nowait,
                                       │  _evaluate_subagent_inbox_output (delayed policy)
   "[System: ...BANANA]" ◀────────────┘  consume-once
```

**Trace evidence (`conv_fc47380ccbff481abf452a446ec4e40d`, summary edges):**
- `tool:sys_session_send` (payload: `{"agent":"worker","title":"banana","args":{"input":"Reply
  with exactly the word BANANA...","purpose":"implement"}}`) — orchestrator→worker dispatch.
- `omni-server -> omni-runner [POST /v1/sessions] x4` — child Conversation minting (the child is
  `conv_263ec4375e7f4ec796c39699ce9fcbda`).
- `omni-runner -> omni-server [GET /v1/sessions/{id}/child_sessions] x1` — the spawn-vs-continue
  existence check (`_find_existing_child_session`).
- `omni-server -> omni-runner [POST /v1/sessions/{conv}/events] x3` — child user-message + parent
  wake.
- Wake notice payload captured verbatim: `"[System: sub-agent worker/banana finished (completed)
  — 1 result waiting in inbox. Call sys_read_inbox to collect.]"` → matches
  `app.py:7471` exactly.
- `tool:sys_read_inbox x2` — first returns `"Inbox is empty — no completed tasks."` (drain before
  child finished), second returns the BANANA result `"[System: sub-agent task
  conv_263ec...completed — worker:banana returned: BANANA]"`. Confirms consume-once + the
  empty-inbox sentinel (`_drain_inbox:5495`).

**Channels in summary:**
- Spawn / create / message / child-enumeration: **runner→server REST** over the WS tunnel
  (`server_client` httpx). Durable: conversation rows. Transient: `session.created` /
  `child_session.updated` SSE on the parent stream.
- Result return: **runner-internal `asyncio.Queue`** (no network) + a **wake POST** runner→server
  →runner to relaunch the parent turn so it drains.
- `sys_session_send` itself reaches the runner as an **Omnigent MCP tool** (`POST /mcp` +
  `/mcp/execute` in the trace) — the orchestrator's harness sees it as a normal tool call;
  `should_dispatch_locally` routes it to `_execute_subagent_tool` (`tool_dispatch.py:204,497`).

---

#### 5. CUJ behaviors (per harness/client)

**Spawn-or-continue (named mode), all harnesses with the Omnigent tool surface.**
1. LLM calls `sys_session_send(agent, title, args)`. `args` may be a bare string or
   `{input, purpose, model, harness, cost_budget}` (object form; `purpose` is what Polly's
   guardrail keys on — `tool_dispatch.py:878`).
2. Runner resolves parent `agent_id` (local cache → `GET /v1/sessions/{id}`), checks for an open
   child by `(agent,title)`, creates one if absent with `agent_id = PARENT's id` (inline
   sub-agents share the parent bundle — `:1402`), posts the user message, returns a `launching`
   handle.
3. Child runs on the **same runner** (affinity; `parent_session_id` co-locates it). At child-turn
   start the runner **swaps `spec` to the child's own sub-spec** via
   `_find_spec_by_name(parent_spec, sub_agent_name)` (`app.py:8717`) — this is what gives the
   child its own harness/model/tools/os_env/**sub_agents**.
4. On completion the runner pushes the payload into the parent's inbox queue and wakes the parent;
   the parent drains via auto-collect or `sys_read_inbox`.

**By-session-id mode** (`session_id` arg): posts to an **existing direct child only** — verified
by `parent_session_id == caller` (`_send_to_existing_session:1629`). Cross-tree / sibling sends
→ `session_out_of_tree`.

**`sys_session_create`**: spawns a child from an `agent_id` (any agent the caller can see) or a
local `config_path` (uploads a fresh **session-scoped** agent). `parent_session_id` hard-forced to
caller. Unlike named send, does **not** block on the child turn (`tool_dispatch.py:1828`).

**Parallel fan-out:** multiple `sys_session_send` calls in one assistant response dispatch
concurrently; each completion lands independently in the inbox.

**TUI vs WebUI:** identical spawn mechanics. The difference is **discovery rail rendering** —
the REPL's `_refresh_subagent_tree` recurses `GET …/child_sessions` to depth
`_MAX_SUBAGENT_TREE_DEPTH=3` (`_repl.py:6266`); the SSE stream only delivers the **active
session's direct children** (`_repl.py:6247`), so grandchildren are kept live only by this poll.
WebUI has its own `SubagentsPanel.tsx`/`useChildSessions.ts`.

**⚠️ Failure branches (code-confirmed):**
- ⚠️ **Spec-resolution miss → parent-clone fallback (runaway recursion).** If
  `sub_agent_name` doesn't resolve in the parent tree (and isn't the web-researcher), callers
  keep the **parent spec** and boot the child as a full clone of the parent — explicitly called
  out as "runaway recursion via `sys_session_send` when the parent is a coordinator"
  (`workflow.py:2563-2566`).
- ⚠️ **No spawn-time depth cap** (§6 — only the display poll is bounded).
- ⚠️ **`sys_cancel_task`/`sys_cancel_async` cancel sub-agents on the runner path** (via
  `_cancel_subagent_task`, `tool_dispatch.py:5764`) but the in-process `SysCancelTaskTool.invoke`
  returns `task_not_found` for everything (`async_inbox.py:97-126`); a handle the work-registry
  doesn't recognize gets the misleading no-op + "tasks table removed" hint.
- ⚠️ **Missing parent inbox** → completion logged + dropped, returns 503
  `subagent_delivery_not_confirmed` (`app.py:7264-7275, 7441`); the runner backfills
  `sub_agent_name` from the server snapshot on reconnect to limit this (`app.py:10291`).
- ⚠️ **`busy`/`launching`/`running` child** → re-send rejected (`sub_agent_busy`), caller must
  wait for the drain or cancel (`tool_dispatch.py:1318-1332`).
- ⚠️ Native sub-agent completions (claude-native) historically never reaching the orchestrator is
  a known field issue (CUJ-ANALYSIS §2.F failures) — owned by the Native-harness section; the
  inbox/queue path above is the SDK-harness path.

---

#### 6. Answers to the doc questions (code-anchored)

**How subagents are spawned.** The LLM calls the Omnigent tool `sys_session_send`/`create` (NOT a
per-`AgentTool` named tool — AgentTool/SelfAgentTool are *spec types* that translate into nested
`AgentSpec` sub_agents; the LLM-facing dispatch is always the generic `sys_session_send` with a
dynamic `agent` enum, `spawn.py:185`). The runner intercepts it
(`should_dispatch_locally`→`_execute_subagent_tool`), mints a child Conversation via
`POST /v1/sessions` with the **parent's** `agent_id` + `parent_session_id` + `title="<a>:<t>"` +
`sub_agent_name`, posts the first user message, and the child runs the same workflow loop on the
same runner. (`tool_dispatch.py:1146`.)

**Info propagation parent↔child (#5).** *Type-level:* `AgentTool.pass_history=True` snapshots the
parent's `"self"` history into the child as `"parent"`; `pass_histories=[names]` snapshots named
histories (`inner/tools.py:289-294`). *Runtime via `sys_session_send`:* the only thing crossing is
`args.input` (the child's first user message); the child's result is truncated and packaged into
the inbox payload (`output` field, `app.py:7290`). **Siblings/cross-agent never talk directly —
only through the parent.** A parent can *read* a child's transcript with
`sys_session_get_history` and *drive* a child with `sys_session_send`/`session_id`, both
**tree-scoped** to `root_conversation_id`.

**Subagent depth limits — ⚠️ confirmed gap.** There is **no spawn-time depth cap anywhere** (grep
of `tool_dispatch.py`, `spawn.py`, `server/routes/sessions.py` for depth/recursion checks → none).
`_MAX_SUBAGENT_TREE_DEPTH=3` is **display-only** (sidebar poll recursion bound, `_repl.py:6271`).
The only structural guard is `AgentTool.max_sessions` (optional per-tool concurrency cap on named
children, surfaced in the tool description `inner/tools.py:161`) and `SelfAgentTool`'s
**parse-time** strip (`spec/omnigent.py:1307-1310`) which prevents an infinite *parse-time* clone
tree but explicitly **allows runtime recursion** ("Recursion at runtime (a clone spawning another
clone) IS [possible]", `spec/omnigent.py:1284-1287`). So a coordinator agent that self-spawns can
recurse without bound.

**How a custom agent's OWN subagents get initialized.** They are **nested `AgentSpec` nodes in the
same bundle** — not separate registrations. When the runner starts a child turn it deep-resolves
the child's spec out of the parent bundle via `_find_spec_by_name(parent_spec, sub_agent_name)`
(`app.py:8721`) and swaps `spec` to that sub-spec; the sub-spec carries its *own* `sub_agents`, so
the grandchild surface comes for free at the next spawn. Inline sub-agents therefore all share the
parent's single `agent_id`/bundle. `AgentTool` can also reference a separately-registered agent
**by name**, and `SelfAgentTool` clones the parent def minus self-tools.

**How custom agents are stored (3 tiers) + caching.**
1. **ArtifactStore** — content-addressed `.tar.gz` (key `<agent_id>/<sha256hex>`); source of
   truth.
2. **`Agent` DB row** (`entities/agent.py`) — `id`, `name`, `bundle_location`, monotonic
   `version` (bumps on update), `session_id` (None=template/operator agent → `expand_env=True`
   allowed; non-null=session-scoped tenant agent → `expand_env=False`, no `${VAR}` expansion to
   avoid secret leak, `agent_cache.py:74-80`).
3. **Server `AgentCache`** (`runtime/agent_cache.py`) — Tier-1 in-mem `_specs` dict + Tier-2 disk
   extract. **No TTL.** Invalidation is explicit: `evict()` on delete, `replace()` warm-swap on
   update (extract to staging, atomic dict assignment, `rename` into place so readers see old-or-
   new, never empty, `:133-159`). Execution load uses `prune_invalid_sub_agents=True` (version
   skew → drop unknown sub-agent with a WARNING rather than fail the parent, `:30-34`).
   The **runner** has an independent disk cache keyed by `(agent_id, version)`
   (`bundles.py:561`) — two distinct caches.
Created via `omnigent create` / POST bundle; uploads are validated and **reject server-side
`callable:` Python tools** (GHSA RCE guard, `bundles.py:29` `_reject_uploaded_callable_tools`,
recurses sub-agents).

**Intelligent routing (`route_turn`).** `server/smart_routing.py:215`. Fires server-side at
`sessions.py:8650` **only when** the session toggle `cost_control_mode_override=="on"`, no model
chosen yet (`effective_runner_override is None`), and `body.type=="message"` (effectively the
first turn; verdict is persisted as `model_override` so later turns skip the judge). Flow:
`infer_models(harness)` → harness family (`claude`/`gpt`/`pi`) → an **LLM judge**
(`LLMRoutingClient`) that reasons about model *names* directly (haiku<sonnet<opus, nano<mini<base,
higher version = newer) and returns `{model, rationale}` strict-JSON. **No `TIER_TEMPLATES`** — it
picks from the flat per-family `MODEL_LISTS` (`:25`). Fail-open: judge error → `None` (spec
default); hallucinated model → clamp to `available_models[0]` (`:198`). Sub-agents inherit the
parent's routing toggle (`sessions.py:8642-8652`).

**Resume dispatch (which harness gets re-launched).** `omnigent/resume_dispatch.py:39` —
`omnigent resume <conv>` (or picker) reads the conversation's `labels.omnigent.wrapper` (local
store or remote `GET /v1/sessions/{id}`) and re-launches the matching **terminal-native** wrapper:
`_dispatch_wrapper` maps the wrapper label → `run_claude_native` / `run_codex_native` /
`run_pi_native` / cursor / kiro / goose / antigravity / qwen / kimi / hermes (`:201-309`). **Only
terminal-native sessions resume in-process**; anything else (SDK sessions) surfaces a
copy-pasteable `omnigent run --resume <conv> <agent.yaml>` hint (`:179-198`). This file is CLI glue
only — the *transcript reload into the runner* is the Runner/resume section.

**Inbox / async-work mechanics.** Three LLM tools (`tools/builtins/async_inbox.py`), gated on
spec `async:` (defaults **True** — `datamodel.py:782`, `spec/types.py:1537`):
- `sys_call_async(tool, args)` — dispatches a **local Python tool** as a background `asyncio.Task`
  on the runner (`tool_dispatch.py:5529` `_spawn_async_tool`), returns a handle immediately;
  result `put_nowait`'d to the session inbox on completion. (MCP/builtin/client tools and
  `sys_call_async` itself are rejected.)
- `sys_read_inbox()` — non-blocking consume-once drain of the queue (`_drain_inbox:5476`); each
  sub-agent payload passes a **delayed policy eval** (`_evaluate_subagent_inbox_output:5409`,
  fail-closed to a suppression sentinel) before formatting; empty → `"Inbox is empty — no
  completed tasks."` Also auto-collected at every loop-iteration boundary so the LLM sees
  completions even without calling it.
- `sys_cancel_async(handle_id)` / `sys_cancel_task(task_id)` — `sys_cancel_async` cancels a live
  async `asyncio.Task` or routes to the sub-agent work registry; the bare `sys_cancel_task`
  **always returns `task_not_found`** (tasks table removed, `async_inbox.py:97`).

---

#### 7. Reliability gaps / sharp edges (code-confirmed)

1. **No spawn-time recursion/depth bound.** A coordinator agent with a `self` sub-agent (or one
   whose `sub_agent_name` misresolves → parent-clone fallback) can spawn unboundedly deep. Only
   `max_sessions` (per-tool concurrency) and the display poll (depth 3) constrain anything.
   (`workflow.py:2563`, `_repl.py:201`.)
2. **`sys_cancel_task` in-process `invoke` is a no-op**, but the *runner* dispatch path is not.
   `should_dispatch_locally` routes `sys_cancel_task`/`sys_cancel_async` to
   `_cancel_async_tool` → falls through to `_cancel_subagent_task` for sub-agent handles
   (`tool_dispatch.py:5755-5764`), so a sub-agent CAN be cancelled on the runner. The
   `SysCancelTaskTool.invoke` body that returns `task_not_found` unconditionally
   (`async_inbox.py:97-126`) only runs on the in-process (non-runner) path — but that means a
   bare async-tool handle (`sys_call_async`) whose `asyncio.Task` already finished, or any id the
   work-registry doesn't know, gets `task_not_found` with a misleading "tasks table removed" hint.
3. **Inbox is a process-local `asyncio.Queue` with no durable backing.** If the parent's runner
   restarts before a child completion is drained, the queued payload is lost (the row is durable
   but the *delivery signal* is not); `_deliver_subagent_completion` only warns + returns
   `MISSING_PARENT_INBOX` 503 when the queue is absent (`app.py:7264`). Reconnect backfills
   `sub_agent_name` (`app.py:10291`) but not pending inbox payloads.
4. **Spawn-vs-continue scans up to 1000 children and filters locally** — the child-session
   endpoint has no `(tool, session_name)` filter; an agent with many named children pays an
   O(n) fetch per send (`tool_dispatch.py:849-853`).
5. **Per-spec `allowed_harnesses` allowlist for `args.harness` is enforced ONLY at orchestrator
   dispatch** (`tool_dispatch.py:1340-1369`). A direct `POST /v1/sessions` with `harness_override`
   is bounded only by the **global** `OMNIGENT_HARNESSES`, not the per-spec allowlist — explicitly
   noted in-code (`tool_dispatch.py:1340-1345`).
6. **AgentCache has no TTL or size bound** — `_specs` + disk dirs grow until explicit
   `evict`/`replace`. A long-lived server accumulates every agent it ever loaded
   (`agent_cache.py`).
7. **Two independent agent caches** (server `AgentCache` vs runner `bundles.py` per-version dir)
   can diverge on version skew; the runner's is keyed by `(agent_id, version)` so a warm-swap on
   the server doesn't invalidate the runner's copy of the old version.

---

#### 8. Corrections to CUJ-ANALYSIS §2.F

The §2.F summary is mostly right; these claims are wrong or drifted:

1. **`TIER_TEMPLATES` does not exist.** §2.F line 317: "picks a model from `TIER_TEMPLATES`".
   `smart_routing.py` has **no tier abstraction** — it uses flat per-family `MODEL_LISTS`
   (`:25`) and an LLM judge that reasons about model *names* directly. Module docstring even says
   "no tier abstraction" (`smart_routing.py:7-9`). Hallucinated model clamps to
   `available_models[0]`, not `tier[0]`.
2. **"native harnesses not routable (returns None)" is WRONG.** §2.F line 318. `_HARNESS_FAMILY`
   maps `"claude-native":"claude"` and `"codex-native":"gpt"` (`smart_routing.py:51-58`), so
   `infer_models` returns a model list for native harnesses and `route_turn` runs. The real
   nuance (worth stating instead): native harnesses bake `--model` at terminal launch, and the
   routing call only fires on a **message** event with the toggle on, so a routed model is
   persisted as `model_override` but a native session created without it won't pick it up
   mid-stream.
3. **Spawn is `sys_session_send`, not a per-`AgentTool` named tool.** §2.F line 306 implies "LLM
   calls a sub-agent tool → mints a child Conversation". Accurate mechanism: the LLM always calls
   the generic `sys_session_send` (dynamic `agent` enum); `AgentTool`/`SelfAgentTool` are spec
   *types* translated into nested `AgentSpec.sub_agents`. The mint happens on the **runner** via
   `POST /v1/sessions` using the **parent's** `agent_id` — inline sub-agents are not separately
   registered. (CUJ's `inner/tools.py:267,298` anchors are correct for the *type defs*; they are
   NOT the dispatch site — dispatch is `tool_dispatch.py:1146`.)
4. **`SelfAgentTool` prune stops *parse-time* recursion only, not runtime.** §2.F line 313:
   "pruned from the clone to stop `self`-recursion". The strip (`spec/omnigent.py:1307-1310`)
   prevents an infinite *parse-time* clone tree; runtime recursion (a clone spawning another
   clone) is **explicitly still possible** (`spec/omnigent.py:1284-1287`). The §2.F "no
   spawn-time depth cap" conclusion is correct; the SelfAgentTool clause overstates the guard.
5. **`resume_dispatch.py` is at `omnigent/resume_dispatch.py`, not `omnigent/runtime/`** (the
   briefing's anchor path). CUJ §2.F line 337 cites `resume_dispatch.py:39` without a dir and is
   effectively correct; flagging because the task brief pointed at `runtime/resume_dispatch.py`
   which does not exist.

(Confirmed-correct §2.F claims worth keeping: 3-tier storage + "no TTL, evict on delete, warm-swap
on update"; `pass_history`/`pass_histories`; siblings-only-via-parent; `async_work_complete`
consume-once; `sys_cancel_task` no-op `task_not_found`; resume reads the wrapper label.)

---

### Component: WEB UI (React SPA under `web/src/`)

All anchors verified against `<WT>=/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`.
Coverage: static analysis of `web/src` + the server-side endpoint traces (`webui-endpoints` corpus
row, conv `conv_32db…`). No live browser; the browser-origin `omni-web` OTel span is out of scope.

#### 1. Role & boundaries

The WebUI is a **pure SPA client of `omni-server`'s `/v1` REST + SSE + WS surface.** It never talks
to a runner, harness, or host directly — every byte goes through one choke point (`lib/host.ts:138`
`hostFetch`: standalone → `fetch`; embedded → host-injected fetcher) and `resolveWebSocketUrl`
(`host.ts:143`) for WS. It owns: the optimistic-send UX, the **streaming↔durable reconciliation**
(`lib/blockStream.ts` + `store/chatStore.ts`), the sidebar/projects/inbox caches (TanStack Query),
and client-derived "Working…" state. It does NOT own conversation history (server is source of
truth), turn execution, policy evaluation, or any persistence — it re-derives a view and POSTs
intents. Two render modes share all of the above: **standalone** (served by omni-server or Vite
proxy) and **embedded** (a host supplies `fetcher`/`resolveWebSocketUrl`/`searchUsers` via
`OmnigentHostConfig`, `lib/host.ts`).

#### 2. Key files & entrypoints (verified path:line)

- `store/chatStore.ts` — Zustand module-scope store; the heart. `send` `:777`, `sendSlashCommand`
  `:995`, `stop` `:1124`, `switchTo` `:1171`, `submitApproval` `:1283`, `bindStream` `:1783`,
  `startStreamPump` (reconnect loop) `:2542`, `pumpStreamEvents` `:2900`, `reconcileOnReconnect`
  `:2394`, `handleSessionEvent` (the `session.*` SSE side-effect switch) `:3412`,
  `patchConversationStatusInCache` `:3979`. Lives outside React so the SSE stream survives remounts.
- `lib/blockStream.ts` — hand-port of `sdks/python-client/omnigent_client/_stream.py`. The reducer
  state machine; **dedup-by-itemId is at the store layer, not here** (this file is a pure block
  factory). `processEvent` `:342`, `closeText`/`closeReasoning` `:251`/`:217`, tool-call dedup
  `:495`, `message_done` race-dedup `:682`. Class `BlockStream.reduce` `:932`.
- `lib/sse.ts` — SSE byte-stream parser (`parseSseStream` `:102`, getReader not for-await, iOS
  Safari < 17.4 bug) + `parseEvent` `:269` (the **authoritative event taxonomy**, ~40 types).
- `lib/sessionsApi.ts` — typed `/v1/sessions` client (REST + the SSE open). `createSession` `:394`,
  `forkSession` `:495`, `switchSessionAgent` `:545`, `launchRunner` `:576`, `updateSession`(PATCH)
  `:629`, `getSessionSlim` `:746`, `fetchSessionItemsPage` `:782`, `fetchInitialHistoryWindow`
  `:833`, `postEvent` `:883`, `openSessionStream` `:921`, `interrupt` `:939`, `stopSession` `:950`,
  `approve` `:967`.
- `lib/sessionUpdatesSocket.ts` — singleton WS client for `WS /v1/sessions/updates` (sidebar push).
  Frame types `:23`, watchdog `HEARTBEAT_WATCHDOG_MS=70_000` `:48`, reconnect backoff `:50`.
- `hooks/SessionUpdatesProvider.tsx` — wires WS frames into the `["conversations"]` /
  `["project-sessions"]` query caches; derives the watch-set `:159`.
- `hooks/useConversations.ts` — sidebar infinite query (`fetchConversationsPage` `:178`, 20/page,
  `order=desc sort_by=updated_at`), project hooks, all rename/delete/archive mutations.
- `hooks/useSessionState.ts` — **sidebar-row badge ONLY** (`getSessionState` `:21`); awaiting >
  running > none. (NOT the chat "Working…" — see §6.)
- `hooks/useSessionLiveness.ts` — open-session liveness truth table (`useSessionLiveness` `:187`).
- `hooks/useRunnerHealth.ts` — socket-down fallback poll of `GET /health?session_ids=` `:95`.
- `lib/capabilities.ts` (`resolveServerInfo` → `GET /v1/info` `:96`) + `lib/CapabilitiesContext.tsx`.
- `lib/identity.ts` — `resolveIdentity` → `GET /v1/me` `:59`; `authenticatedFetch` `:126`
  (injects `X-Forwarded-Email`, `cache:"no-store"`, 401→login redirect).
- ChatPage working-state helpers: `computeIsWorking` `pages/ChatPage.tsx:4684`, `computeShowsWorking`
  `:4705`, `shouldShowWorkingIndicator` `:2346`.

#### 3. Internal model (chatStore)

The store splits **reactive render state** from **internal bookkeeping** (`ChatState`,
`chatStore.ts:198`). Core:

- `blocks: AnyBlock[]` (`:222`) — the flat list the renderer walks. Holds committed history
  (hydrated by `itemsToBlocks`) **and** streaming output appended at the tail. **Single dedup key:
  `block.ctx.itemId`.**
- `pendingUserMessages: PendingUserMessage[]` (`:223`) — optimistic user bubbles POSTed but not yet
  acked by `session.input.consumed`. Held OFF `blocks` so a prior turn's streaming output appends
  cleanly. Each has a client `tempId` (`pend_<n>`, NOT the server id — keeps React key stable across
  the POST), a `posted` flag, and an `author`.
- `pendingByConversation: Record<id, StashedPending>` (`:246`) — per-conversation stash of **only
  this client's UNSETTLED (`posted!==true`) own bubbles**, so an in-flight send survives in-app
  navigation. A baseline `committedTexts` (`StashedPending`, `:150`) prevents the "disappears then
  reappears" dedup bug on resumed sessions with prior history.
- `activeResponse: {responseId, state, error}` (`:248`) — lifecycle of the in-flight turn.
- `status: "idle"|"streaming"` (`:258`, UI-local send-in-flight) vs `sessionStatus: SessionStatus`
  (`:273`, server-authoritative `idle|launching|running|waiting|failed`, seeded on bind, driven by
  `session.status` SSE). These are **distinct** — `sessionStatus` adds `waiting` (parent parked on
  async-work drain) which `status` can't represent.
- `isNativeTerminalSession` (`:286`) — derived from the `omnigent.wrapper` label on bind; gates the
  whole optimistic-bubble lifecycle (native messages reconcile via transcript round-trip, can arrive
  after a transient idle).
- History window: `hasMoreHistory`/`oldestItemId`/`historyGeneration` (`:355`,`:363`,`:493`) — bind
  hydrates one windowed page; scroll-up `loadMoreHistory` pages older. `historyGeneration` is a
  monotonic guard that voids in-flight page reads after a window reset.
- Plus ~25 snapshot-hydrated session fields (model/effort/cost/usage/todos/skills/viewers/sandbox/
  terminalPending), each updated by a matching `session.*` SSE event.

Module-scope singletons: `sendChain` (`:581`, serializes POSTs in submit order),
`pendingInitialPrompts` map (`:694`, NewChatDialog→ChatPage first-message handoff).

#### 4. Inter-component channels (every edge in/out)

The WebUI's only peer is **omni-server**. Three transports:

```
 ┌─────────┐  REST /v1/* (hostFetch, X-Forwarded-Email)          ┌────────────┐
 │ WebUI   │ ───────────────────────────────────────────────────▶│            │
 │ (SPA)   │  SSE  GET /v1/sessions/{id}/stream  (per-conv tail)  │ omni-server│
 │         │ ◀═══════════════════════════════════════════════════│            │
 │         │  WS   /v1/sessions/updates  (sidebar push, full-state)│            │
 │         │ ◀────────────────────────────────────────────────▶  │            │
 │         │  WS   /v1/sessions/{id}/resources/terminals/{t}/attach│            │
 │         │ ◀────────────────────────────────────────────────▶  └────────────┘
 └─────────┘   (terminal xterm bytes; TerminalView.tsx:443)
```

The runner/harness/host edges (`POST /v1/sessions`, `/agent/contents`, `/skills`,
`/policies/evaluate`, the `/v1/runners/{id}/tunnel` WS) seen in the corpus are **server↔runner**, not
client-facing — the WebUI never sees them. Trace evidence (`summary_conv_32db…`): the
`webui-endpoints` GET battery hit `GET /v1/sessions/{id}` ×11, `/items` ×5, plus
`/agent`, `/policies`, `/skills` — all received by `omni-server` (the runner-bound work rides the
tunnel WS, `tree_conv_32db…` trace 4/19).

##### Durable vs streaming per channel

| Channel | Direction | Carries | Durable? |
|---|---|---|---|
| `SSE /v1/sessions/{id}/stream` | server→client | `response.*` (task-scoped: text/reasoning/tool deltas, lifecycle) **+** `session.*` (session-scoped: status/usage/presence/resource/elicitation) | **streaming** (no replay buffer); `[DONE]` sentinel = clean close |
| `WS /v1/sessions/updates` | bidir | client `{type:"watch", session_ids}`; server `snapshot`/`changed`/`removed`/`heartbeat` (full-row, never field deltas) | sidebar list freshness |
| `WS …/terminals/{t}/attach` | bidir | raw PTY bytes (xterm.js ⇄ tmux) | live only |
| `GET /v1/sessions/{id}/items` | client→server | committed conversation items (paginated, `order=desc`) | **durable** (source of truth) |
| `POST /v1/sessions/{id}/events` | client→server | intents: `message`/`slash_command`/`interrupt`/`approval`/`stop_session`/`compact` | item-typed persisted before 202 returns |

##### The COMPLETE set of API requests the WebUI sends (exhaustive, from hooks/stores)

**Boot / identity / capabilities**
- `GET /v1/info` — `lib/capabilities.ts:104` (accounts/sandbox/databricks/version gates).
- `GET /v1/me` — `lib/identity.ts:64` (current user; 401→`login_url` redirect).
- `GET /api/version` — `lib/host.ts` / info popover.
- `POST /auth/login`, `/auth/logout`, `/auth/setup`, `/auth/register`, `/auth/invite`,
  `/auth/magic/redeem`, `GET /auth/me`, `/auth/users…/password|reset` — accounts mode
  (`lib/accountsApi.ts`, LoginPage/RegisterPage/MembersPage). Only reachable when `accounts_enabled`.

**Sidebar / projects / discovery**
- `GET /v1/sessions?order=desc&sort_by=updated_at&limit=20[&after=][&search_query=][&include_archived=][&project=]`
  — `useConversations.ts:201` (infinite), `fetchAllProjectSessionIds` `:727`, `fetchProjectSessionIds`
  `:753`, `useAgents.fetchAgents` (`?limit=100` `:82`).
- `GET /v1/sessions/{id}` — `fetchConversationById` (pinned backfill) `:151`.
- `GET /v1/sessions/projects` — `useProjects` `:665`.
- `WS /v1/sessions/updates` — `sessionUpdatesSocket.ts:66` (sole sidebar live channel).

**Open a conversation (bind)** — see §7 ordering
- `GET /v1/sessions/{id}?include_items=false&include_liveness=false[&refresh_state=true]` —
  `getSessionSlim` `:746`. (Slim! NOT the full `getSession`.)
- `GET /v1/sessions/{id}/items?limit=20&order=desc[&after=]` — `fetchSessionItemsPage` `:782`
  (also `useSessionItems.ts:66`).
- `GET /v1/sessions/{id}/stream[?idle=true]` — `openSessionStream` `:921` (SSE; holding it open
  registers presence; `?idle` is the entire presence uplink).

**Send / control (POST /v1/sessions/{id}/events)** — `postEvent` `:883`
- `{type:"message"}` (send), `{type:"slash_command", data:{kind:"skill"…}}` (skill),
  `{type:"interrupt"}` (`interrupt` `:939`), `{type:"stop_session"}` (`stopSession` `:950`),
  `{type:"compact"}` (`chatStore.compact`), `{type:"approval"}` (legacy approval path).
- `POST /v1/sessions/{id}/resources/files` (multipart) — attachment upload (`filesApi.ts:16`).

**Session lifecycle / mutation**
- `POST /v1/sessions` (JSON or multipart bundle) — `createSession` `:394` / `createBundledSession`
  `:441`.
- `POST /v1/sessions/{id}/fork` — `forkSession` `:495`.
- `POST /v1/sessions/{id}/switch-agent` — `switchSessionAgent` `:545`.
- `POST /v1/hosts/{hostId}/runners` — `launchRunner` `:576` (fork-resume bind).
- `PATCH /v1/sessions/{id}` — `updateSession` `:629` (model/effort/cost/collab/runner/silent),
  `renameConversation` `:249` (title), `archiveConversation` `:266`, `moveConversationToProject`
  `:673` (project label).
- `DELETE /v1/sessions/{id}[?delete_branch=true]` — `deleteConversation` `:283`.
- `GET /v1/runners` — `listRunners` `:688` / `bindOnlyOnlineRunner` `:698`.

**Approvals / policies / permissions / comments**
- `POST /v1/sessions/{id}/elicitations/{eid}/resolve` — `approve` `:967` (primary approval path;
  ApprovePage deep-link uses the same).
- `GET /v1/policy-registry` `usePolicies.ts:42`; `GET/POST /v1/sessions/{id}/policies` `:35`/`:83`;
  `DELETE …/policies/{pid}` `:107`; `GET/POST/DELETE /v1/policies[/{id}]` (admin PoliciesPage).
- `GET/POST/DELETE /v1/sessions/{id}/permissions[/{userId}]`, `GET …/owner` —
  `lib/permissionsApi.ts:96-130`.
- `GET/POST/DELETE/PATCH /v1/sessions/{id}/comments[/{commentId}]`, `POST …/comments/send` —
  `useComments.ts:47-164` (Inbox `useCommentInbox` reuses).
- `PATCH /v1/sessions/{id}/read-state` — unread tracking.

**Files / agent introspection / codex / health**
- `GET /v1/sessions/{id}/resources/environments/{env}/filesystem/{path}` — file content
  (`useFileContent.ts`); `…/diff/{path}` — `useFileDiff.ts`; changed-files + dir listing.
- `GET /v1/hosts/{id}/filesystem`, `…/directories` — new-chat workspace browser (`useHostFilesystem.ts:127,203`).
- `GET /v1/sessions/{id}/agent` (+ `/agent/mcp-servers[/…]`) — `useAgents.ts:134,196`.
- `GET/PUT /v1/sessions/{id}/codex_goal[/status]` — `lib/codexGoalApi.ts:152,178,199`.
- `GET /health?session_ids=…` — `useRunnerHealth.ts:95` (socket-down liveness fallback).
- `WS /v1/sessions/{id}/resources/terminals/{tid}/attach[?read_only=true]` — `TerminalView.tsx:443`.
- `POST <otel>/v1/traces` — browser telemetry (`lib/telemetry.ts:44`, the out-of-scope `omni-web`
  span source).

**NOT a client REST call:** user search is host-IoC (`getOmnigentUserSearch`, `useUserSearch.ts`) —
the SPA never calls a `/v1/users/search` endpoint itself (correction to CUJ-ANALYSIS).

#### 5. CUJ behaviors

##### Send → optimistic bubble → durable promotion (`send` `:777`)
1. Push a `pend_<n>` bubble to `pendingUserMessages` BEFORE the POST (renders instantly).
2. `ensureBoundSession` (`:1539`): for a brand-new session → `createSession` + `bindStream` +
   `opts.onConversationCreated` (navigate `/`→`/c/:id`) then POST; for an existing session whose
   stream died → rebind first so response events have a subscriber.
3. Upload files (real `file_id`s), then `postEvent {type:message}`. Serialized through `sendChain`
   so rapid sends reach the server in order.
4. On 202: mark bubble `posted:true`, drop its stash copy. On `denied:true`: roll back the bubble
   from the POST response (no `session.input.consumed` will ever come). On throw: roll back + append
   a client error block (or mark the active response failed).
5. The bubble clears when `session.input.consumed` promotes it to a committed `blocks` entry
   (`handleSessionEvent` `:3724`), matched: (1) by `clearedPendingId`, (2) FIFO head, (3) fresh
   render — keeping the same React key (`stableKey=tempId`, no remount).

##### Streaming↔durable reconciliation (the core Q) — `pumpStreamEvents` `:2900`
- The pump taps the raw SSE for `session.*` side effects (`tapSessionEvents`→`handleSessionEvent`)
  and live-delta previews (`tapLiveDeltas`, claude-native) BEFORE handing the rest to
  `BlockStream.reduce`.
- **Dedup is enforced in three places, all keyed by `ctx.itemId`:** (a) at emit time the pump skips
  any itemId already in `blocks` or in the rAF buffer (`:3004`); (b) at flush commit-time it
  re-checks itemIds because a snapshot merge can race a buffered block in (`:2961`); (c) `bindStream`
  filters snapshot blocks against `state.blocks`' itemIds (`:1907`). Elicitations dedup by
  `elicitationId` instead (not persisted items, `:3018`).
- **The streaming↔persisted merge point:** a streamed assistant `text_done` is initially id-less; the
  relay later re-publishes it as `output_item.done` carrying the store id. The pump stamps that id
  **onto the already-rendered streamed block in place** (`:3027`) rather than appending — so the live
  view keeps one copy in its streamed position and reconnect (itemId-keyed) sees it as rendered.
- Rendering is rAF-coalesced (`createRafScheduler` `:2667`); first content of each response paints
  synchronously (`paintedFirstContent` `:2939`), the rest batch.

##### Working vs idle (the Q) — see §6.

##### Close page & return — see §7.

##### Stop / interrupt (`stop` `:1124`)
Fire-and-forget `POST {type:interrupt}`; the local SSE stream stays open. Server emits
`session.interrupted` (→ `interruptedResponseIds`, marks bubble cancelled) + `response.incomplete`.
Optimistically patches the sidebar row idle (⚠️ unbacked write `:1160` — a poll mid-turn can briefly
revert the dot; self-corrects on the real idle).

##### Fork / switch-agent
Fork: `POST …/fork` → new unbound session; ForkSessionDialog then `launchRunner` to bind. Switch:
`POST …/switch-agent` keeps the same session; the `session.agent_changed` SSE re-derives
`isNativeTerminalSession` via `refreshSessionBinding` (`:3517`) since the URL doesn't change.

##### ⚠️ Failure branches I can confirm in code
- Stream open 401/403/404 → give up, `sessionStatus:"failed"`, no infinite spinner (`:2600`).
- `POST /events` 503 (runner never came online) → typed `ApiError.code`, standalone error block
  appended so the user sees WHY (`:982`).
- Background-tab throttling can drop `elicitation_resolved`; a `visibilitychange` listener
  reconciles pending cards against a fresh snapshot on re-show (`bindStream` `:1800`).
- `session.superseded` (Claude `/clear`) drops the superseded conv's pending bubbles and redirects
  via `redirectToConversationId` (`:3848`) — else the `/clear` bubble spins forever.

#### 6. Answers to the doc questions

**How "working vs idle" is derived in the client & whether it agrees with the server.**
TWO independent derivations:
- **Chat surface "Working…":** `computeIsWorking(sessionStatus)` = `sessionStatus∈{running,waiting}`
  (`ChatPage.tsx:4684`); the display gate `computeShowsWorking` (`:4705`) additionally OR-s
  `backgroundTaskCount>0` and suppresses on `runnerOnline===false` / pending elicitation.
  `sessionStatus` is **server-authoritative** — seeded from the snapshot on bind and updated only by
  `session.status` SSE events (`handleSessionEvent` `:3585`). So the chat agrees with the server by
  construction. The local `status` flag is a separate "is a send in flight" latch.
- **Sidebar row badge:** `getSessionState` (`useSessionState.ts:21`) reads the **list row's**
  `status` + `pending_elicitations_count` (awaiting > running > none) — these come from
  `GET /v1/sessions` / the WS updates stream, NOT the chat store. The store's
  `patchConversationStatusInCache` (`:3979`) mirrors the chat's live `session.status` into the active
  row so the dot doesn't lag a poll behind the chat indicator (it mirrors the server's own
  running/waiting→"running" collapse, so it never fights the poller).
  **(CUJ-ANALYSIS conflates these — see §8.)**

**How the WebUI reconciles streaming vs durable into one coherent view.** See §5. One `blocks` list;
dedup-by-`ctx.itemId` at three layers; `pendingUserMessages` held off `blocks` until
`session.input.consumed`; the streamed↔persisted text merge stamps the durable id onto the streamed
block in place.

**The ENTIRE set of API requests.** Enumerated exhaustively in §4.

**How the sidebar fetches items (pagination + live updates).** `useConversations` infinite query
(`:229`): cursor-paginated `GET /v1/sessions`, 20/page, `order=desc sort_by=updated_at`,
`getNextPageParam=last_id`. Live updates ride `WS /v1/sessions/updates` (`SessionUpdatesProvider`):
client pushes a watch-set (every cached conversation id + the open session, even off-sidebar
children, `:177`); server replies `snapshot` then `changed`/`removed`/`heartbeat` (full rows). The
provider patches matching rows in place (`mergeItemsIntoPages`) and falls back to a debounced
`invalidateQueries(["conversations"])` for structural/membership/sort changes it can't reconstruct
locally (`:249`). HTTP poll is the fallback only: `false` while the socket is connected (unless a
list opts into 60s reconcile), `45s` when disconnected (`:240`). A 70s silence watchdog
(`HEARTBEAT_WATCHDOG_MS`) force-reconnects a silently-dead socket.

**What happens when you close the page and come back.** Server is durable; the session keeps running
while the page is closed. On return, `switchTo`→`bindStream` (`:1783`): (1) open the SSE stream
FIRST (`startStreamPump`), (2) concurrently fetch the **slim** snapshot
(`getSessionSlim{refresh_state:true}`) + the initial windowed history page
(`fetchInitialHistoryWindow` — `max(1 page, back-to-previous-user-prompt)`), (3) merge snapshot
blocks into whatever the pump already pushed, deduping by itemId, and replay `pending_elicitations`
+ `pending_inputs` from the snapshot (the SSE stream has no replay buffer, so a prompt fired while
away only re-renders from the snapshot). Stream-first-then-snapshot is the documented reconnect
contract — events that arrived before the snapshot are deduped, events after are kept. A reconnect
(not first connect) additionally drops the stale in-flight bubble and runs `reconcileOnReconnect`
(`:2394`), which pages backward up to `RECONNECT_BACKFILL_MAX_PAGES` until the fetched window
overlaps the pre-gap transcript, then splices missed committed items + recovers elicitation state
the dead socket swallowed.

**TUI-vs-WebUI state differences (web side).** Both clients POST the same `/v1/sessions/{id}/events`
intents and consume the same SSE vocabulary, so transcript content converges. Web-specific state the
TUI has no analog for: the optimistic `pendingUserMessages`/stash machinery (the REPL prints
synchronously), the rAF flush scheduler, presence (`?idle` uplink via stream reconnect), the
`WS /v1/sessions/updates` sidebar push (TUI re-lists on demand), and the live-delta provisional
preview for claude-native (`live:<msgId>` blocks). The client↔server credential is
`X-Forwarded-Email` injected by `authenticatedFetch` (web) vs the REPL's own header.

#### 7. Reliability gaps / sharp edges (confirmed in code)

- **`stop()` optimistic sidebar patch is unbacked** (`:1155-1160`): unlike the SSE-driven caller, no
  server event backs it, so a `useConversations` poll interleaving while the turn is genuinely still
  running can briefly revert the sidebar dot. Self-corrects on the real idle. Documented in-code.
- **SSE has no replay buffer** — every transient (`response.error`, elicitation, presence) is lost if
  the client isn't subscribed. Mitigations are snapshot-replay (`pending_elicitations`,
  `last_task_error`→synthetic error block `:2024`) and the visibility reconcile, but a transient with
  no durable equivalent (e.g. a mid-turn `response.retry`) is simply gone on reconnect.
- **Native-terminal idle race:** `session.status:idle` deliberately does NOT clear pending bubbles for
  `isNativeTerminalSession` (`:3665`) because the transcript-forwarder `session.input.consumed` can
  arrive after a transient idle; correctness here leans on the server-side `pending_inputs` TTL. If
  that event is permanently lost, the bubble relies on the next snapshot dedup to clear.
- **Two reconnect/backoff implementations** (chatStore stream pump `:2107` and
  sessionUpdatesSocket `:50`) with the same 250ms/5s/jitter constants duplicated — drift risk.
- **Databricks Apps ingress caps a single HTTP/2 stream at ~5 min** (`:589`): the SSE pump treats a
  drop-without-`[DONE]` as reconnectable and re-subscribes instantly after a healthy connection
  (`failedOpens` stays 0). A reader/parse error (`net::ERR_HTTP2_PROTOCOL_ERROR`) is also a "dropped",
  not a failure, so a routine recycle stays invisible.
- **`fetchInitialHistoryWindow` cap** (`MAX_INITIAL_PAGES=8`, `:805`): a pathological single turn
  spanning >8 pages opens with `hasMore:true` and the prompt above the response possibly not loaded
  until scroll-up — bounded, not silent truncation.

#### 8. Corrections to CUJ-ANALYSIS §2.E

1. **§2.E "Working/idle state" (lines 261-262) is wrong about the source.** It says
   `hooks/useSessionState.ts` derives THE working/idle state from `status` +
   `pending_elicitations_count`. That file is **the sidebar-row badge ONLY** (`getSessionState`,
   `useSessionState.ts:21`, header comment explicitly says so). The **chat surface** "Working…"
   comes from the chat store's `sessionStatus` (driven by `session.status` SSE) via
   `computeIsWorking`/`computeShowsWorking` in `ChatPage.tsx:4684/4705` — a different field, a
   different code path, server-authoritative. The two should be documented separately.
2. **§2.E "Close page & return" (line 254) says refresh refetches `GET /sessions/{id}`.** It actually
   calls `getSessionSlim` — `GET /v1/sessions/{id}?include_items=false&include_liveness=false&refresh_state=true`
   (`sessionsApi.ts:746`), NOT the full snapshot — and crucially **opens the SSE stream FIRST**, then
   the slim snapshot + a *windowed* history page (not full history). Items come from
   `GET …/items` (paginated), not embedded in the session response.
3. **§2.E "Streaming↔durable reconciliation" (lines 258-260) locates dedup-by-itemId in
   `lib/blockStream.ts`.** `blockStream.ts` is a **pure block factory** (hand-port of `_stream.py`);
   it carries no `blocks` array and does no itemId dedup. Dedup-by-`ctx.itemId` lives in
   **`chatStore.ts`** at three sites: pump emit-time (`:3004`), flush commit-time (`:2961`), and
   `bindStream` snapshot merge (`:1907`). The streamed↔persisted *merge point* (stamping the durable
   id onto the streamed block in place) is `pumpStreamEvents:3027`.

Minor: §2.E table (line 455) labels WebUI live-in as "`WS /health/subscribe`" — the actual
socket-down liveness path is an HTTP **poll** `GET /health?session_ids=` (`useRunnerHealth.ts:95`),
not a WS subscribe; and user search is host-IoC, not a `GET /v1/users/search` the bundle issues.

---

### Component: TUI / REPL (`omnigent run` interactive client + python-client SDK)

All anchors verified against the worktree `/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`.
Trace corpus: every conv was driven headless via this exact `omni run` client, so the
client→server REST/SSE surface IS the TUI surface. Caveat: the Jaeger traces are *server-side*
spans — the REPL is not an instrumented service, so its calls appear in the op-list **un-parented**
(`GET …/stream`, top-level `POST …/events`, `POST …/fork`), while the *parented* `omni-runner ->
omni-server` edges (`GET …/items`, `…/agent/contents`) are the RUNNER's transcript fetches, NOT
the REPL's.

---

#### 1. Role & boundaries

The TUI/REPL is a **pure HTTP client of the Omnigent server**. It owns: the interactive
terminal loop (rich streaming render, slash commands, @-file completer, resume picker, theme
picker, Ctrl+E event tape, Ctrl+O debug overlay, sub-agent rail), the `run`/`attach` CLI dispatch
+ topology selection, and the python-client SDK (`OmnigentClient` + namespaces) that wraps every
REST/SSE call. It does **not**: own a runner (the daemon does), execute the agent loop, persist
anything (server is source of truth — `n/a` in the persists column), or talk to the harness/MCP
directly. Its only durable side-effect is an optional client-side JSON conversation dump on exit
(`--log`).

Two code homes:
- **Client REPL/dispatch**: `omnigent/repl/` (`_repl.py`, `_resume_picker.py`, `_event_tape.py`,
  `_session_log.py`, `_theme_picker.py`, `_tmux_pane.py`) + `omnigent/chat.py` + `omnigent/cli.py`.
- **SDK**: `sdks/python-client/omnigent_client/` (`_client.py`, `_sessions.py`, `_sessions_chat.py`,
  `_sse.py`, `_files.py`, …). NOT a decoupled package — it imports `omnigent.server.schemas`
  directly for wire types (`_sse.py:14`, `_sessions_chat.py:42`).
- **Render primitives** (`TerminalHost`, `RichBlockFormatter`, `FileMentionCompleter`, themes) live
  in a *separate* package `sdks/ui/omnigent_ui_sdk/` (`terminal/_host.py`) — prompt_toolkit-based.
  Cross-ref: that package is shared infra, the spinner/working-badge mechanics are there.

---

#### 2. Key files & entrypoints (verified)

- `omnigent/cli.py:6335` `@cli.command run` (decorator), `:6393` `def run(...)` — flags `--server`,
  `--harness`, `--model`, `-p/--prompt`, `-r/--resume` (`flag_value=_RESUME_PICKER_SENTINEL`,
  `:6347`), `-c/--continue` (`:6355`), `--fork` (`:6358`), `--no-session`/`ephemeral` (`:6359`),
  `--log`, `--debug-events` (`:6372`), `--host` (no-op, `:6481` `del register_host`).
- `omnigent/cli.py:6253` `@cli.command attach` → `chat.run_attach` (pure co-drive client; never spawns).
- `omnigent/cli.py:5899` `_dispatch_run` — topology router (direct-server vs local-agent; `:5961`
  rejects URL-as-AGENT; `:5996` reroutes interactive `--server <url> --resume <id>` to `run_attach`).
- `omnigent/chat.py:254` `run_chat` — the 3-way branch: `:366` URL→`_chat_with_server`;
  `:401` ephemeral→`_chat_local`; `:425` else→`_chat_via_daemon` (default).
- `omnigent/chat.py:850` `_chat_with_server` (→`_run_repl` or `_run_one_shot`),
  `:1722` `_chat_via_daemon`, `:3805` `_run_repl` (the `OmnigentClient(...)` construction at
  `:3962` + `run_repl(...)` call at `:3981`), `:4017` `_run_one_shot`.
- `omnigent/repl/_repl.py:2936` `async def run_repl(client: OmnigentClient, ...)` — the REPL.
- `omnigent/repl/_repl.py:1234` `class _SessionsChatReplAdapter` — the duck-typed `Session` the
  REPL drives (NOT `SessionsChat`; it calls `client.sessions.*` directly).
- `omnigent/repl/_repl.py:3230` `_render_session_event` (push renderer), assigned to
  `session._on_event` at `:3789`.
- `sdks/python-client/omnigent_client/_client.py:21` `OmnigentClient` (single `httpx.AsyncClient`
  at `:89`, SSE read-timeout 600s `:74`, sentinel `Origin` header `:86` to pass the server CSRF
  guard on multipart routes).
- `sdks/python-client/omnigent_client/_sessions.py` — `SessionsNamespace`: `create:338`,
  `list:394`, `bind_runner:450`, `unbind_runner:481`, `set_reasoning_effort:504`,
  `set_model_override:535`, `set_archived:580`, `list_items:648`, `child_sessions:684`,
  `child_sessions_tree:717`, `subtree_busy:765`, `get:793`, `post_event:814`,
  `resolve_elicitation:845`, `fork:887`, `compact:941`, `interrupt:959`, `stream:980`
  (`_stream_session_events:1018`).
- `sdks/python-client/omnigent_client/_sse.py:86` `parse_sse_stream` — `event:`/`data:`/`[DONE]`
  framing → typed events.
- `omnigent/repl/_resume_picker.py:197` `pick_conversation`, `:878` `pick_conversation_from_sdk`
  (`client.sessions.list(...)`).
- `sdks/ui/omnigent_ui_sdk/terminal/_host.py:204` `_SPINNER_FRAMES`, `:224` `_SubagentNode`
  (busy/last_task_error → web-parity Working/Failed badge).

---

#### 3. Internal model

The REPL builds **one** `_SessionsChatReplAdapter` (`_repl.py:3133`) per run, duck-compatible with
the legacy `Session` (`send/cancel/model/current_response_id/is_streaming/reset/
resume_from_response/set_reasoning_effort/set_model_override`). Core state (`__init__` `:1251`):

- `_session_id` (None until first send → `create`, or pre-set on resume/attach).
- `_session_bundle` — gzipped agent tarball; required to `create` a fresh session, also kept so the
  one-shot path can take its sessions branch. `None` on a pure URL/attach target.
- `_runner_id` / `_bound_runner_id` — runner affinity; `_attach_only` / `_readonly_view` /
  `_interactive_child` flags gate whether this client may PATCH the binding.
- `_stream_task` — the **persistent SSE pump** (`_stream_pump`, `:2030`). `_recover_task` — the
  runner-recovery watchdog. `_on_event` — push render callback. `_turn_done` — asyncio.Event the
  pump sets on terminal status so `send()` returns.
- Dedup counters: `_pending_local_user_sends` (`:1378`) for optimistic user-message echo;
  `_saw_text_deltas` + `_TurnProseTracker` (`_repl.py:3164`) for assistant prose.

**Lifecycle** (`_ensure_session`, `:1659`): `create` (multipart `POST /v1/sessions` →
`GET /v1/sessions/{id}` snapshot) OR resume (`get` snapshot) → `_hydrate_from_session_snapshot`
(`:1613`) → `_bind_runner_if_needed` (PATCH, owner-only) → spawn `_stream_pump` + recover watchdog →
`_notify_session_start_once` (fires the open-in-browser callback). Idempotent under a lock.

---

#### 4. Inter-component channels (every edge in/out)

The TUI talks to **exactly one peer**: the Omnigent server (`omni-server`), over **REST + one
long-lived SSE**. No WS, no UDS, no tmux from the REPL itself (tmux is only `_tmux_pane`
pane-registration for sibling-pane re-launch, not a data channel).

##### Outbound REST (client → server) — full enumeration
| Method+path | SDK call | purpose | durable? |
|---|---|---|---|
| `POST /v1/sessions` (multipart `metadata`+`bundle`) | `sessions.create` | upload ephemeral agent, mint session; returns `{session_id}` only | yes (server) |
| `GET /v1/sessions/{id}` | `sessions.get` | snapshot/refresh (status, model, harness, context_window, runner) | read |
| `GET /v1/sessions` (cursor, `agent_name=`) | `sessions.list` | resume picker / `-c` newest-lookup | read |
| `GET /v1/sessions/{id}/items` (paginate, `order=asc`, 100/pg) | `sessions.list_items` | durable transcript replay (resume/attach + Ctrl+O overlay) | read |
| `GET /v1/sessions/{id}/child_sessions` (+ `/child_sessions` tree) | `sessions.child_sessions(_tree)`, `subtree_busy` | sub-agent rail polling | read |
| `GET /v1/sessions/{id}/agent` | `client._fetch_agent_tools` | spec tool list (client-tool validation) | read |
| `PATCH /v1/sessions/{id}` | `bind_runner`/`unbind_runner`/`set_model_override`/`set_reasoning_effort`/`set_archived`/`set_external_session_id` | runner affinity + per-session config; last-write-wins | yes |
| `POST /v1/sessions/{id}/events` | `post_event` (+`send`,`compact:941`,`interrupt:959`) | `message` / `function_call_output` / `compact` / `interrupt` / `approval`; 202 ack `{queued,item_id}` | message persists |
| `POST /v1/sessions/{id}/elicitations/{eid}/resolve` | `resolve_elicitation` | MCP `ElicitationResult` verdict (URL path, owner-gated) | n/a |
| `POST /v1/sessions/{src}/fork` | `fork` | deep-copy items → new session | yes |
| `POST /v1/sessions/{id}/files` (multipart) | `files.for_session(id).upload` | attachment upload before a `send` with `files=` | yes |
| `GET /api/version` | version probe | — | read |

##### Outbound SSE (client → server, long-lived)
- `GET /v1/sessions/{id}/stream` — `sessions.stream` → `_stream_session_events:1018`. **No replay
  buffer** server-side (`stream` docstring `:989`). The REPL's `_stream_pump` (`:2030`) loops it
  forever: on `[DONE]` reopen after 0.5s; on transport error reconnect with exp backoff
  (0.5→5s, `:2031`) and re-bind the runner. Every event → `_on_event` (push render).

##### Inbound (server → client) — typed SSE events
Parsed by `_sse.py:_parse_event` into `omnigent.server.schemas.ServerStreamEvent`. Lifecycle
`response.{created,queued,in_progress,completed,failed,incomplete,cancelled}`; `session.status`
(`running/idle/waiting/failed`); `session.input.consumed` (echoed user msg → dedup);
`session.agent_changed`; `session.child_session.updated` (sub-agent rail); `response.output_text.delta`;
`response.reasoning_{started,text.delta,summary_text.delta}`; `response.output_item.done`
(function_call ± `action_required`, function_call_output, message, native-tool); `response.output_file.done`;
`response.compaction_in_progress`; `response.elicitation_request` / `elicitation_resolved`;
`response.client_task.cancel`; `response.retry`; `response.error`.

##### Auth on the channel (client↔server credential)
`OmnigentClient(auth=_server_auth(...))` (`_repl.py:3965`) → `_DatabricksTokenAuth` (`chat.py:687`),
an `httpx.Auth` that runs on **every** request (incl. each SSE reconnect): static
`OMNIGENT_REMOTE_AUTH_TOKEN` → stored OIDC token from `omnigent login` → Databricks SDK token
(resolved ONCE, cached in-memory, CLI re-shell only near expiry, `:711`). `X-Databricks-Org-Id`
selector set first regardless of branch. ⇒ token refresh is transparent and per-request; **no
hook-style fail-closed expiry** like the native path (cross-ref native-hook memory).

##### Trace evidence
`summary_conv_84f9…` / `_conv_32db…`: `GET …/stream x2` (REPL pump open + 1 reconnect),
`GET …/items x4-5`, `POST /v1/sessions x3-4`, top-level `POST …/events x1` (REPL user msg, distinct
from the parented `omni-server -> omni-runner POST …/events` turn-dispatch and
`omni-runner -> omni-harness POST …/events`), `conv_32db…` shows `POST …/{source_id}/fork x1` (the
fork CUJ). The `omni-runner -> omni-server GET …/items|…/agent/contents` edges are the runner's
transcript load (cross-ref runner section), NOT the REPL.

---

#### 5. CUJ behaviors

**Topologies of `omni run` (the headline question):**
```
(a) DEFAULT  `run <agent.yaml>`  → _ensure_backend → host DAEMON ensured (local persistent
    server spawned if none) → _chat_via_daemon: upload bundle, daemon spawns+OWNS the runner
    bound to the session, REPL attaches as pure HTTP client. CLI never spawns/tears down the
    runner; daemon relaunches a dead runner server-side; runner_recover=None (chat.py:1862).
        REPL --REST/SSE--> server <--WStunnel--> runner(daemon-owned, local) --> harness/MCP(local)

(b) `run <agent.yaml> --server <remote URL>`  → local-agent + remote-server (RUNNER.md Flow 1).
    The CLI/daemon path uploads the YAML as an ephemeral session and a LOCAL runner tunnels to the
    remote server (so terminals/MCP run on the laptop). [Same _chat_with_server REPL surface.]

(c) `run --server <url>` (NO AGENT)  → DIRECT-CLIENT mode (_dispatch_run:5968): `run_chat(
    target=base_url)` → _chat_with_server → _pick_agent (GET agents) → _run_repl. No local runner;
    turns dispatch to the server's host-bound runner (co-drive, like WebUI).
        REPL --REST/SSE--> server (server's own runner)

(d) `run … --no-session`  → _chat_local: in-process EPHEMERAL local server (subprocess, tmpdir
    SQLite) + a SIBLING runner over a loopback WS tunnel (chat.py:3512 _start_cli_runner_process,
    binding_token → token_bound_runner_id:3519). Persistence in a per-run tmpdir.

(e) `attach <id>`  → _chat_with_server(attach_only=True): pure co-drive, NEVER binds a runner
    (adapter._attach_only short-circuits all bind PATCHes, _repl.py:1777), fails loud if host offline.
```

**Streaming↔durable reconciliation (the Q) — how the TUI merges SSE + durable into one view:**
1. **Resume/attach** → `GET /items` paginated replay renders the *durable* past (one-time, oldest-first).
2. **Live turns** → the persistent `_stream_pump` pushes SSE deltas to `_render_session_event`.
3. **Dedup of the seam** (TUI's analog of WebUI's `ctx.itemId` dedup, but *counter-based not id-based*):
   - User message: `send()` optimistically increments `_pending_local_user_sends` (`:2171`) and the
     local echo is rendered immediately; when the server's `session.input.consumed` for that same
     user msg arrives over the stream, it is **swallowed** (decrement, `:3384-3385`) instead of
     re-rendered.
   - Assistant prose: streamed `TextDelta`s set `_saw_text_deltas`; the later `output_item.done`
     `message`(role=assistant) is suppressed if it matches already-streamed prose
     (`_TurnProseTracker`, `_repl.py:3586`). `_flush_inflight_assistant_text` (`:3171`) commits a
     text segment at each tool-call boundary so interleaved prose+tools don't re-render the whole
     turn (the "growing preamble" bug).
4. **Status → working/idle** (next Q).

**Working / idle in the TUI:** driven by `session.status` SSE events, NOT a poll. `status=="running"`
→ `host.start_timer()` (`_repl.py:3290`, braille spinner `_host.py:204` @10Hz + live elapsed
seconds, e.g. `2.3s` `:672`); `status in {idle,waiting,failed}` → `host.stop_timer()` +
`_turn_done.set()` (`:3318-3346`). A SETUP-phase failure emits only `session.status:failed` (no
`response.failed`) — rendered as an error line (`:3329`) else the spinner would just vanish. Separately,
a **"N agents running" badge + sub-agent tree** is computed from `ChildSessionSummary.busy` /
`last_task_error` (`_host.py:248,252`) delivered via `session.child_session.updated` SSE +
`GET /child_sessions` polling, with a 3s linger debounce (`_SUBAGENT_LINGER_SECONDS`); same
"busy" definition as the web `SubagentsPanel`. `subtree_busy` (`_sessions.py:765`) rolls the whole
subtree for headless drivers.

**Resume picker / `-r` / `-c` / `--fork`:** resolved to a concrete conversation_id *before* the REPL
opens (`chat.py:_resolve_resume_target:2654`). Precedence: explicit `--resume <id>`
(`_assert_resume_conversation_exists`, fail-loud on bad id) > bare `-r` picker
(`_run_picker`→`pick_conversation_from_sdk`→`client.sessions.list(agent_name=)`, cancel→fresh) > `-c`
newest (`_resolve_latest_conversation_id`, no-prior→ClickException). The id becomes
`resume_conversation_id`; the adapter then `get`s + replays `/items` instead of `create`-ing.
`--fork` (`_run_repl:3970`) calls `client.sessions.fork(src)` first, lands the REPL on the fork id,
prints the back-pointer. ⚠️ Local-only flags (`-c`/`-r`/`--log`/`--no-session`) are **rejected**
against a bare remote-URL target (`chat.py:379`) — only `--resume <id>` works remotely (attach).
⚠️ **Wrapper-aware resume redirect** (`chat.py:953`, `_dispatch_run:5999`): if the conv was created by
a `*-native` TUI wrapper, the AP REPL is the wrong surface — it re-dispatches into the native wrapper
(via the `omnigent.wrapper` label) instead of rendering an empty chat over a tmux-owned session.

**Interrupt / queue:** `Esc` → `adapter.cancel()` → `interrupt` → `POST …/events {type:interrupt}`
(server co-emits `response.incomplete reason=user_interrupt` + `session.interrupted`). A `send()`
while a turn runs just POSTs another `message` — the server queues it (the client only observes events).

**Elicitations / approvals (client side):** an inbound `response.elicitation_request` (parsed
`_sse.py:255`) wakes the `on_elicitation_request` hook → the REPL collects the form via the main
input loop (`_FieldInputState`, schema-driven prompts `_repl.py:2482`) → `resolve_elicitation`
(`POST …/elicitations/{eid}/resolve`). External resolution (web Approve page) publishes
`elicitation_resolved`, which wakes the parked future (`_repl.py:3407`). Cross-ref policy/server
sections for which hooks make all policies work.

**Sub-agent dive (`↓` selector):** `view_session` (`:1937`) cancels+restarts the pump on the child
id WITHOUT moving the runner binding (`_readonly_view=True` suppresses PATCH; `_interactive_child`
lets you co-drive a child by POSTing to *its* runner). The TUI shows **one** session's stream at a
time. `/switch` (`switch_to_session:1894`) is the heavier op that unbinds old + binds this REPL's
runner onto the new session.

**Client-side tools** (`--tools coding`): the pump sees `output_item.done` `function_call`
`status==action_required` → `_spawn_client_tool` runs the local callable → POSTs
`function_call_output` to unblock the parked turn (`_repl.py:3551`, mirrors
`_sessions_chat.py:_maybe_dispatch_tool_call:693`).

**Close page & return:** server-durable; closing the REPL leaves the daemon-owned session running.
Re-`attach`/`resume` reopens via `get` + `/items` replay + fresh stream.

---

#### 6. Answers to the doc questions (terse, code-anchored)

- **How TUI reconciles streaming vs durable into one view:** §5 above — `/items` one-time durable
  replay + persistent SSE pump for live; seam dedup via `_pending_local_user_sends` (user) and
  `_saw_text_deltas`/`_TurnProseTracker` (assistant prose). `_repl.py:2171,3384,3586`.
- **TUI vs WebUI state differences:** TUI uses ONLY `GET /sessions/{id}/stream` SSE + REST; it has
  **no `WS /sessions/updates`** (no live sidebar) and **no `WS /health/subscribe`**. Sub-agent rail
  is `GET /child_sessions` polling, not the updates WS. TUI views one session's stream at a time
  (`view_session` re-points the pump); WebUI keeps the tree + sidebar live via WS. WebUI optimistic
  bubble keyed by `ctx.itemId`; TUI by a `_pending_local_user_sends` counter. No projects/pin/
  comments/presence/members surfaces in the TUI.
- **Full REST+SSE request set the TUI sends:** the §4 table — `POST /v1/sessions` (multipart),
  `GET /v1/sessions{,/{id},/{id}/items,/{id}/child_sessions{,_tree},/{id}/agent}`, `GET /api/version`,
  `PATCH /v1/sessions/{id}` (bind/unbind/model/effort/archived/external-id),
  `POST /v1/sessions/{id}/events` (message/function_call_output/compact/interrupt/approval),
  `POST /v1/sessions/{id}/elicitations/{eid}/resolve`, `POST /v1/sessions/{src}/fork`,
  `POST /v1/sessions/{id}/files`, and the long-lived `GET /v1/sessions/{id}/stream` SSE.
- **Resume picker / `-r` / `-c`:** §5 — resolved to a conversation_id pre-REPL in
  `_resolve_resume_target` (chat.py:2654); precedence id > picker > latest.
- **Local-runner-vs-direct-client topologies:** §5 (a)-(e). Default = daemon-owned runner; remote
  `--server <url>` + AGENT = local runner tunnels (Flow 1); `--server <url>` no-AGENT = direct
  client onto server's runner; `--no-session` = in-proc ephemeral server + sibling loopback runner;
  `attach` = pure co-drive, no runner ownership.
- **How "working" is shown:** §5 — `session.status` running/idle gates `host.start/stop_timer`
  (spinner + elapsed); subtree badge from `ChildSessionSummary.busy`. `_repl.py:3290,3318`,
  `_host.py:204,248`.
- **`--debug-events` pipeline:** enables the `Ctrl+E` event tape overlay
  (`_event_tape.record_raw`/`update_translation`/`update_format`/`mark_rendered` woven through every
  render branch), JSONL event log under `~/.omnigent/debug/`, and pipeline-stage counters in the
  bottom toolbar (`run_repl` docstring `_repl.py:2984`; tape calls e.g. `:3276,3305`).

---

#### 7. Reliability gaps / sharp edges (confirmable in code)

- **No SSE replay buffer + reconnect gap.** `stream` has no server replay (`_sessions.py:989`); the
  pump reconnects but **transient SSE-only events landing in a reconnect gap are lost**. The code
  mitigates by re-syncing snapshot metadata at each turn start (`_spawn_metadata_refresh`,
  `_repl.py:3316`) and re-deriving on `session.agent_changed` — but e.g. a one-off elicitation or a
  status blip in the gap can be missed (comments at `:3314`, `:5645`).
- **Counter-based user-msg dedup is fragile.** `_pending_local_user_sends` assumes the locally-sent
  message and the streamed `session.input.consumed` arrive 1:1 in order. A dropped/duplicated echo
  off-by-ones it → either a double-rendered user line or a swallowed real one. (Conceptually the same
  class as the native first-message FIFO desync in memory, but a different code path — worth a note.)
- **Resume requires the local bundle to create, not to attach.** `_ensure_session:1668` raises if
  `session_id is None and session_bundle is None`. A pure URL target with no bundle can only attach to
  an existing id, never start fresh — the error text steers to `omnigent run <agent.yaml>`.
- **`_server_headers(runner_id)` is a no-op** (`chat.py:814` `del runner_id`): runner affinity is now
  carried by `PATCH /v1/sessions/{id}`, not a request header. Any doc/claim that the client passes
  `runner_id` as a header is stale.
- **600s fixed SSE read timeout, `timeout=` param ignored** (`_client.py:69` accepted-but-ignored).
  A turn that holds the stream silent >600s (very long tool call with no interim events) trips a
  read-timeout reconnect; harmless (server continues) but logs churn — classified recoverable
  (`_is_recoverable_sse_transport_error`, `:74`).
- **Attach fails loud if host offline** (intended), but the only signal a sub-agent dive
  (`view_session`) gives on a child whose runner died is silence — the rail polls `/child_sessions`,
  the child's own stream carries no `child_session.updated` about itself (comment `_repl.py:6191`).

---

#### 8. Corrections to CUJ-ANALYSIS

1. **§2.E "TUI/REPL equivalents" line (`_repl.py` anchor) — verified but underspecified.** The doc
   says `run_repl` does "rich streaming, slash commands, file-mention completer, resume picker, theme
   picker, event tape". TRUE, but it omits the **central architectural fact**: the REPL drives a
   custom `_SessionsChatReplAdapter` (`_repl.py:1234`) over `client.sessions.*` with a **persistent
   single SSE pump** — NOT the SDK's `SessionsChat` helper (which opens a fresh stream per `send`).
   The streaming-vs-durable reconciliation lives in `_render_session_event` (`:3230`) +
   `_pending_local_user_sends`/`_TurnProseTracker`, not in the SDK.
2. **§5 table row 454 (TUI/REPL "REST out") is incomplete.** It lists only `POST /sessions`,
   `/events`, `GET /sessions/{id}`, interrupt/approval. The TUI ALSO sends `PATCH /sessions/{id}`
   (bind/unbind runner + model_override + reasoning_effort + archived), `POST /sessions/{src}/fork`,
   `POST …/elicitations/{id}/resolve`, `GET …/items`, `GET …/child_sessions(_tree)`, `GET …/agent`,
   `GET /api/version`, multipart `POST …/files`, and `POST /v1/sessions` is **multipart bundle**
   create. Its REST breadth ≈ the WebUI's; the real TUI-vs-WebUI gap is the **`SSE/WS in` column**:
   TUI has **only** `SSE /sessions/{id}/stream` and (correctly `—` for WS out) but **lacks
   `WS /sessions/updates` and `WS /health/subscribe`** — those are WebUI-only. The "SSE/WS in: SSE
   /sessions/{id}/stream" entry for TUI is right; add "(no WS)".
3. **Briefing/CUJ `_client.py:89` anchor — path correction.** `OmnigentClient` is NOT under
   `omnigent/`; it lives in the separate `sdks/python-client/omnigent_client/_client.py` (class at
   `:21`, the single `httpx.AsyncClient` at `:89`). The line number 89 happens to match the
   AsyncClient, but the file path in some references reads as `omnigent/.../client.py`
   (`omnigent/llms/client.py` is an unrelated LLM client) — the SDK path is the correct one.
   Also: render primitives (`TerminalHost`, formatter) are in yet another package
   `sdks/ui/omnigent_ui_sdk/`, not in `omnigent/repl/`.

(Minor) §2.F `repl/_repl.py:_MAX_SUBAGENT_TREE_DEPTH=3` "display-only": consistent with what I see —
the TUI sub-agent rail/`subtree_busy` cap is a display/poll depth (`max_depth=3`), not a spawn cap.
Not re-verified line-for-line here (out of my component), flagging as plausibly-correct.

---

### Credentials / Auth / Onboarding

> Verified against worktree `master-arch-docs` @ `3a0128df` (main + telemetry PR #1617).
> All `path:line` anchors below were opened and confirmed. Scope: claude (sdk+native),
> codex (sdk+native), Polly (inherits its harness). pi/goose/cursor/etc. cross-referenced only.

#### 1. Role & boundaries

This component owns **THREE distinct credential relationships** and the first-run setup that
populates them. They are independent — different stores, different refresh mechanics, different
failure modes:

| # | Relationship | Credential | Store | Refresh |
|---|---|---|---|---|
| (1) | **LLM creds** (harness → model provider) | api-key / subscription / Databricks OAuth bearer / `auth_command` | `~/.omnigent/config.yaml` `providers:` block; secrets via `env:`/`keychain:` refs; CLI logins in `~/.claude`,`~/.codex`,`~/.databrickscfg` | per-request for Databricks (SDK), static for api-key |
| (2) | **runner ↔ server** (callbacks + WS-tunnel + policy POSTs) | Databricks OAuth bearer **or** `omnigent login` session JWT | `~/.omnigent/auth_tokens.json` (JWT) + Databricks CLI OAuth cache | per-request (httpx) / per-reconnect (WS) |
| (3) | **client ↔ server** (TUI/Web/CLI → server identity) | `__Host-ap_session` cookie (browser) or `Bearer <JWT>` (CLI) | browser cookie jar; CLI `~/.omnigent/auth_tokens.json` | **none** — token expires, user re-`login`s |

**Owns:** provider selection at setup (`onboarding/`), the provider/model resolution chain, the
three creds' resolution + refresh, the **native policy-hook token path** (the historically-buggy
one), and credential/catalog caching.

**Does NOT own:** policy *evaluation* (→ policies/server component), the WS-tunnel transport
itself (→ runner/transport component), session identity propagation past `get_user_id` (→ server
routes). It produces the bearer; downstream components consume it.

#### 2. Key files & entrypoints (all verified)

**Onboarding / setup:**
- `omnigent/onboarding/wizard.py` — interactive first-run picker. `run_wizard_and_launch()` @ `wizard.py:1384`. Detected-CLI menus `_build_agent_labels`/`_show_coding_agents_and_pick` @ `wizard.py:851,888`; Databricks profiles hint @ `wizard.py:543`; OPENAI_API_KEY/BASE_URL ambient detection @ `wizard.py:1108-1137`.
- `omnigent/onboarding/ambient.py` — ambient CLI/key detection. `DetectedKind = key|subscription|local|cli-config` @ `ambient.py:41`; Claude subscription via `~/.claude/.credentials.json` (Linux file) / macOS Keychain + `claude auth status` fallback @ `ambient.py:12-15,122-130`; codex via `~/.codex/auth.json` `codex_auth_has_credential` @ `ambient.py:143` and `~/.codex/config.toml` `[model_providers.X]` @ `ambient.py:208-215`.
- `omnigent/onboarding/setup.py` — Databricks profile **aliasing**. Discovers via `databricks auth profiles --output json` `_existing_profile_hosts` @ `setup.py:109-149`; `_alias_source_for` finds a profile already pointing at the host @ `setup.py:160`; `_alias_profile` copies the cfg section (inherits login, skips a redundant OAuth dance) @ `setup.py:190`; `detect_conflicting_env_vars` strips shadowing `DATABRICKS_*` @ `setup.py:89-99`.
- `omnigent/onboarding/providers/__init__.py` — provider catalog + default-model rules (see §6).
- `omnigent/onboarding/provider_config.py` — the `providers:` YAML parser + secret resolution.

**LLM creds + refresh:**
- `omnigent/inner/databricks_executor.py` — `_DatabricksBearerAuth(httpx.Auth)` @ `:289`, `.auth_flow` @ `:367` (per-request `Config.authenticate()`); `_resolve_databricks_auth` @ `:384`, `_resolve_databricks_auth_for_host` @ `:509` (profile-pinned-to-host preferred over `--host` token lookup).
- `omnigent/inner/codex_executor.py` — codex-databricks AI Gateway: `_databricks_codex_auth_command` @ `:730`, baked as `auth.command` @ `:2175-2178`; model precedence comment @ `:2198`.
- `omnigent/onboarding/provider_config.py:244-258` — `auth_command` field (one of `{api_key, api_key_ref, auth_command}`, mutually exclusive @ `:583-595`); `resolve_secret` (`env:`/`keychain:`) @ `:420`.
- `omnigent/model_catalog.py` — `resolve_model_provider(spec, harness)` @ `:301`; precedence delegated to `runtime/workflow._resolve_provider_for_build` @ `:343`.

**runner ↔ server:**
- `omnigent/runner/_entry.py` — `_RunnerDatabricksAuth(httpx.Auth)` @ `:162`, `.auth_flow` @ `:192` (per-request + retry on 401 **or** Apps 302→`/oidc/`); `_make_auth_token_factory` @ `:271` (resolution order: stored OIDC JWT → Databricks SDK bearer); `_is_login_redirect_or_unauthorized` @ `:241`.
- `omnigent/runner/transports/ws_tunnel/serve.py` — `serve_tunnel` @ `:238`; `_refresh_auth_token` called **before each (re)connect** @ `:284`; header set at `websockets.connect(additional_headers=…)` @ `:543-545`.

**client ↔ server:**
- `omnigent/server/auth.py` — `resolve_auth_source` @ `:193`, `UnifiedAuthProvider` @ `:250`, `_check_cookie` (cookie → Bearer fallback, TTL cache) @ `:351`, `_check_header` @ `:415`, `create_auth_provider` @ `:461`.
- `omnigent/cli_auth.py` — `omnigent login` storage in `~/.omnigent/auth_tokens.json` (`_TOKEN_FILE_NAME = "auth_tokens.json"` @ `:29`); `store_token`/`load_token` (JWT, **expiry-checked, no refresh**) @ `:84,166-188`; `store_databricks_auth`/`load_databricks_workspace_host` (Apps pointer, **no token stored**) @ `:109,191`; `databricks_request_headers` (Authorization + `X-Databricks-Org-Id`) @ `:229`.

**Policy-hook token path (the ⚠️ one):**
- `omnigent/native_policy_hook.py` — shared hook↔policy translation. `policy_hook_wrapper_script` bakes one-shot token into `_OMNIGENT_AUTH_HEADERS` @ `:103-130`; **`policy_hook_reauth` re-mint factory** @ `:133-168`; `post_evaluate_with_retry(..., reauth=)` re-mints once on 401/302 @ `:434,500-522`; `fail_closed_hook_output` (PreToolUse→deny, UserPromptSubmit→block, PostToolUse→open) @ `:383-431`.
- Per-harness hooks pass `reauth=policy_hook_reauth(...)`: `claude_native_hook.py:657,729,881`; `codex_native_hook.py:170`; `kimi_native_hook.py:158`.
- `omnigent/runner/app.py:1144-1149` (opencode), `:3657-3665` (claude), `:12386-12408` — one-shot snapshot taken at launch; cost-popup mints **fresh** to dodge staleness @ `:12254-12257,12393-12399`.

#### 3. Internal model

**`providers:` config (config.yaml).** Parsed by `provider_config._parse_provider` @ `:748`. Each
`ProviderEntry` has a `kind` ∈ `{key, subscription, local, gateway, databricks, cli-config, bedrock}`
and one credential form: inline `api_key` (`$VAR` expanded), `api_key_ref` (`env:<VAR>` /
`keychain:<name>`, resolved lazily by `resolve_secret` @ `:420`), or `auth_command` (a `sh`
command that prints a bearer). Per-**family** defaults (`anthropic` / `openai` / `pi` surface):
at most one `default: true` per family, enforced in `get_default_provider` @ `:1071-1098`.

**Live example (this user's config.yaml — matches code paths exactly):**
```
anthropic       kind=key          api_key_ref=env:ANTHROPIC_API_KEY   # static, no refresh
claude          kind=subscription cli=claude         (default)        # ~/.claude OAuth, claude-owned
codex-databricks kind=cli-config / gateway            auth.command="databricks auth token --profile oss"
databricks      kind=databricks   profile=oss                         # per-request SDK refresh
openai          kind=key          api_key_ref=env:OPENAI_API_KEY      # static
```
The **codex-databricks** path routes through the Databricks AI Gateway: `codex_executor.py:2175-2178`
bakes `_databricks_codex_auth_command(host, "oss")` into `~/.codex/config.toml` as `auth.command`,
so **codex itself** shells out to `databricks auth token --profile oss` on its own token lifecycle.
**This is the live LLM-cred refresh failure mode:** the `oss` profile's OAuth refresh token is
expired/revoked → `databricks auth token` returns empty → codex sends no/blank bearer → gateway
401s → the turn fails. Note `provider_config.py:746-758`: the command uses `--force-refresh` *only*
if the CLI supports it; plain `auth token` still auto-refreshes an expired **access** token — so the
failure is specifically a dead **refresh** token, not a normal expiry. Fix is `databricks auth login --profile oss`.

**`auth_tokens.json` (CLI/client store).** Two record shapes keyed by server URL (`cli_auth.py:1-16`):
a session-JWT record `{token, user_id, expires_at}` (`load_token` returns `None` past `expires_at` @ `:183` — **no refresh**), or a Databricks-Apps pointer `{auth_type:"databricks", workspace_host, org_id}` that **stores no token** (bearers minted fresh from the host-keyed Databricks CLI OAuth cache). `0o600` perms (`:81`).

**`UnifiedAuthProvider` (server-side identity).** One instance per server, closed over by route
factories. Mode chosen once at boot by `resolve_auth_source` @ `:193`. Holds a `_cookie_cache:
dict[digest → (user_id, monotonic_expiry)]` (`auth.py:310,387-411`) so repeated requests skip JWT
decode for the token's remaining lifetime.

#### 4. Inter-component channels

```
 CLIENT (TUI/Web/CLI)                SERVER                          RUNNER                 PROVIDER
   |  cookie __Host-ap_session  ->  UnifiedAuthProvider.get_user_id   |                        |
   |  (Web)                         (oidc/accounts mode)              |                        |
   |  Authorization: Bearer <JWT> ->  _check_cookie Bearer fallback   |                        |
   |  (CLI: omnigent login)           (CLI clients)                   |                        |
   |                                                                  |                        |
   |                                <- 401 + login_url (Web redirect) |                        |
   |                                                                  |                        |
   |                          POST/GET callbacks (httpx)        <-----|  _RunnerDatabricksAuth |
   |                          Authorization: Bearer <DBX|JWT>         |  per-request + 401/302 |
   |                          + X-Databricks-Org-Id                   |  retry                 |
   |                                                                  |                        |
   |                          WS /v1/runners/{id}/tunnel        <-----|  serve_tunnel header   |
   |                          Authorization minted at connect        |  refreshed per-reconnect|
   |                                                                  |                        |
   |                          POST /v1/sessions/{id}/policies/evaluate <-- native hook subproc |
   |                          Authorization (baked snapshot,          |  reauth() on 401/302   |
   |                           re-minted on 401/302)                  |                        |
   |                                                                  |   per-request bearer ->|  LLM API
   |                                                                  |  _DatabricksBearerAuth |  (DBX) /
   |                                                                  |  OR static api-key     |  static key
```

**Trace evidence (`conv_32db3f5927d9459fa028cbe69d4173d3`, claude-sdk + policy):**
- `omni-runner -> omni-server [POST /v1/sessions/{id}/policies/evaluate] x2` and `policy.evaluate x3` spans — this is relationship (2)/policy-hook channel. The captured `policy.content` payloads show PHASE_REQUEST (the prompt) and PHASE_TOOL_RESULT going to the server. (Confirms the runner-side relay path, not the native-hook subprocess, for SDK harnesses — SDK harnesses POST via the in-process runner client, not a `/bin/sh` hook.)
- `HTTP /v1/runners/{id}/tunnel websocket receive/send` (x98/x30) — the WS-tunnel whose `Authorization` header relationship (2) mints once per connection.
- **No repeated `Config.authenticate` / OAuth-flow spans** — expected: the local stack has no Databricks creds, so the per-request Databricks path (1)/(2) is a no-op there. Per-request Databricks auth would show as repeated auth shell-outs against a real workspace.

#### 5. CUJ behaviors (per harness/client)

**First-run setup (`omnigent` with no config):** `run_wizard_and_launch` @ `wizard.py:1384` →
ambient detection (`ambient.py`) surfaces logged-in CLIs (claude/codex subscription), env keys
(ANTHROPIC/OPENAI/GEMINI), local Ollama, and `~/.databrickscfg` profiles → user picks a coding
agent → config.yaml `providers:` block written with the chosen `kind`. Databricks selection runs
`setup.py` aliasing so an existing profile's login is reused (`_alias_profile` @ `:190`).

**LLM-cred resolution at session/turn start (all harnesses):** model string resolves
**spec.model > provider default > catalog default** (`codex_executor.py:2198` states it verbatim;
fail-loud if a neutral gateway has none @ `:2196-2204`). The *provider entry* resolves via
`resolve_model_provider` @ `model_catalog.py:301` → `_resolve_provider_for_build`
(`runtime/workflow.py`) which is the precedence the spawn-env builders + native launch share;
per-harness legacy fallthrough @ `model_catalog.py:358-381` (claude-sdk reads `auth:` blocks +
profiles; codex/pi read ONLY `config["profile"]` + `databricks-*` model prefix). Default *provider*
per harness: `default_provider_for_harness` @ `provider_config.py:1126` (maps harness→family→
`get_default_provider`; pi falls back anthropic→openai skipping subscription/bedrock).

**LLM bearer at request time:**
- Databricks provider/gateway → `_DatabricksBearerAuth.auth_flow` calls `Config.authenticate()`
  **on every HTTP request** (`databricks_executor.py:367`). SDK serves cached OAuth from memory;
  re-shells to the CLI (~0.5s) only near expiry (`:329-331`). OAuth refresh transparent → sessions
  outlive the 1h access-token lifetime.
- api-key / subscription → static; no refresh path (subscription's refresh is the CLI's own, opaque to Omnigent).

**Client ↔ server (3) per mode** (`resolve_auth_source` @ `auth.py:193`):
- `header` (default; Databricks Apps / oauth2-proxy): read `X-Forwarded-Email` (overridable
  `OMNIGENT_AUTH_HEADER`, strip-prefix for IAP). Missing header → **401 fail-closed**, except an
  explicit single-user loopback (`OMNIGENT_LOCAL_SINGLE_USER=1`) falls back to `"local"` @ `:456`.
- `oidc`: `__Host-ap_session` cookie minted by authorization-code+PKCE; redirect `/auth/login`.
- `accounts` (OSS default when `OMNIGENT_AUTH_ENABLED=1`, no OIDC): same cookie, minted by
  username/password `/auth/login`; redirect SPA `/login`.
- **CLI clients** (TUI over REST/`omnigent login`): no cookie → `Authorization: Bearer <JWT>`
  fallback in `_check_cookie` @ `:381-383`. The JWT comes from `auth_tokens.json`; **`omnigent
  login` has no background refresh** — when the JWT expires the user must re-run `login`.

**Token refresh — chat path vs policy-server path (the ⚠️ historical bug):**
- **Chat / runner-callback path** is refresh-capable everywhere: `_RunnerDatabricksAuth.auth_flow`
  re-mints on 401 **and** on the Apps `302→/oidc/` (`_entry.py:192-238`); the WS-tunnel re-mints
  per reconnect; `_DatabricksBearerAuth` re-mints per request.
- **Native policy-hook path** was the asymmetric gap: the `/bin/sh` wrapper bakes a **one-shot**
  bearer into `_OMNIGENT_AUTH_HEADERS` at launch (`native_policy_hook.py:103-130`); that token dies
  with the ~1h Databricks OAuth lifetime. Old behavior: hook only checked 401, but the Apps front
  door bounces an expired bearer with a 302 (not 401), so after ~1h **every tool call failed CLOSED**
  ("policy evaluation unavailable") while chat kept working. **CURRENT STATE = FIXED:**
  `post_evaluate_with_retry` now takes a `reauth` callable and, on 401 **or** 302→`/oidc/`,
  re-mints via `policy_hook_reauth` (same `_make_auth_token_factory`) and retries once
  (`native_policy_hook.py:500-522`). All in-scope python hooks wire it: **claude
  (`:657,729,881`), codex (`:170`)** — also kimi (`:158`). ✅ This matches the briefing's PR #1439
  (and the broader #1482 that swept all 5 python hooks).
  - ⚠️ **Remaining gap (out of my scope, cross-ref):** `pi_native` is a Node hook, not python — it
    does not import `_make_auth_token_factory` and so cannot re-mint (per project memory
    "native-hook-reauth-landscape"). Only claude/codex/Polly are in scope and all refresh.
  - Note SDK harnesses (claude-sdk, codex) **don't** use the `/bin/sh` hook at all — they POST
    `/policies/evaluate` via the in-process runner client, which carries the refresh-capable auth
    (see trace `conv_32db…` `policies/evaluate` edges). The hook fail-closed concern is native-only.

#### 6. Answers to doc questions (terse, code-anchored)

**Provider selection at setup:** ambient detection (`ambient.py`) classifies each source as
`key|subscription|local|cli-config`; the wizard (`wizard.py:851-941`) lists detected coding agents
+ keys + Databricks profiles and the user picks; selection is written to config.yaml `providers:`
with a `kind`. Databricks picks run profile **aliasing** (`setup.py:160-211`) to reuse an existing
login.

**Default model/provider resolution chain:** model string = **spec.model > provider default
(`surface_default_model`/`default_chat_model`) > catalog default** (`codex_executor.py:2198`).
Provider *entry* = `resolve_model_provider` (`model_catalog.py:301`) → `_resolve_provider_for_build`
(shared precedence: explicit spec `auth:`/model_provider, then config `providers:` default for the
harness's family, then legacy per-harness fallthrough). Per-harness default model pins live in
`providers/__init__.py`: `_DEFAULT_MODEL_OVERRIDE` @ `:424-433` (`anthropic→claude-opus-4-8`,
`openai→gpt-5.5`, `openrouter→moonshotai/kimi-k2.6`, `xai→grok-3`) wins over the dynamic
catalog rule; `_PREFERRED_DEFAULT_TIER_TOKEN` @ `:415-417` steers anthropic to `sonnet` (broadly
accessible) when no override; otherwise newest non-specialty chat model (`default_chat_model` @ `:436`).

**Refresh of all three creds:**
1. **LLM creds:** Databricks (provider/gateway) = **per-request** via SDK `Config.authenticate()`
   (`databricks_executor.py:367`), transparent OAuth refresh, in-memory cached, CLI re-shell only
   near expiry. codex-databricks = codex shells `databricks auth token --profile oss` on its own
   cadence. api-key / subscription = **static** (no Omnigent-side refresh).
2. **runner↔server:** httpx callbacks = **per-request** (`_RunnerDatabricksAuth`, re-mint on
   401/302). WS-tunnel = **once per connection**, re-minted **per reconnect** only
   (`serve.py:284`) — NOT mid-connection, but the tunnel doesn't outlive token expiry without a
   reconnect because dead bearers bounce the next reconnect. Token source = stored OIDC JWT first,
   else Databricks SDK (`_make_auth_token_factory:276-303`).
3. **client↔server:** Web = `__Host-ap_session` cookie (oidc/accounts), validated + TTL-cached.
   CLI = `Bearer <JWT>` from `auth_tokens.json`, **expiry-checked, no background refresh** — expired
   → user re-runs `omnigent login`. Header mode = stateless (proxy injects identity each request).

**Caching (what / TTL / invalidation):**
- Provider **catalog** (model lists from MLflow GitHub release): `providers/__init__.py` 1h TTL
  (`_CATALOG_TTL_SECONDS = 3600` @ `:102`), `cachetools.TTLCache(maxsize=64)`; no explicit invalidation
  (TTL only). Skipped under `OMNIGENT_DISABLE_CATALOG_LOOKUP=1`.
- Model **listing** (per-credential enumeration): `model_catalog.py` 5min TTL (`_CATALOG_TTL_S =
  300.0` @ `:61`), keyed by **non-secret credential fingerprint** (`_listing_cache_key` @ `:228`,
  `_credential_fingerprint` @ `:208`) so different creds get different listings; invalidated by
  `clear_model_catalog_cache()` @ `:250` after reconfiguring providers.
- **Server JWT cache:** `UnifiedAuthProvider._cookie_cache` keyed by HMAC digest, TTL =
  token's remaining lifetime (`auth.py:387-411`); never explicitly cleared (entry expires with token).
- **Databricks SDK token:** in-memory in the reused `Config` (one per `_make_auth_token_factory`,
  `_entry.py:312-323`); SDK invalidates near expiry.
- Agent cache: **no TTL** (cross-ref the agents component) — out of my scope.

#### 7. Reliability gaps / sharp edges (code-confirmed)

1. ⚠️ **`omnigent login` JWT has no background refresh** (`cli_auth.py:166-188`). A long-lived TUI
   session that outlives the session-JWT expiry starts 401ing on every server call with no
   self-heal — the user must notice and re-`login`. (The Databricks-pointer record dodges this by
   storing no token and minting fresh; the JWT record does not.)
2. ⚠️ **WS-tunnel bearer is minted once per connection, refreshed only on reconnect**
   (`serve.py:284,543`). If a tunnel stays open longer than the OAuth lifetime *without* a
   reconnect, the next server-initiated frame could ride a stale bearer; in practice the per-request
   httpx path (which DOES refresh) carries the real callbacks, and the tunnel reconnect re-mints.
   The asymmetry (per-request refresh vs per-connection refresh) is a latent sharp edge if frames
   ever carry auth-sensitive operations.
3. ⚠️ **codex-databricks dead-refresh-token failure is silent at the Omnigent layer**
   (`codex_executor.py:2175-2178`): the `auth.command` runs inside codex, so when `databricks auth
   token --profile oss` returns empty (revoked refresh token) Omnigent sees only a 401 from the
   gateway, not the root cause. **This is live in the user's config (the `oss` OAuth is expired).**
4. **Native one-shot policy token IS now refresh-capable** for claude/codex/kimi
   (`native_policy_hook.py:500-522`) — but the fix is **per-harness opt-in**: any hook that calls
   `post_evaluate_with_retry` *without* `reauth=` silently keeps the old fail-closed-after-1h
   behavior. pi_native (Node) is the confirmed remaining hole (out of scope here).
5. **Header-mode fail-closed depends on a single env flag.** A misconfigured deploy that sets
   `OMNIGENT_LOCAL_SINGLE_USER=1` on a multi-user server would resolve every header-less request to
   the shared `"local"` identity (`auth.py:456`). The flag is set only by managed local spawn paths,
   but it's a one-flag blast radius.

#### 8. Corrections to CUJ-ANALYSIS

> (CUJ-ANALYSIS.md §2.G "Credentials".) Each item is what I could/couldn't confirm against code:

1. **CLI token file name.** Some docs/notes refer to `~/.omnigent/n` or imply a different
   filename. **Confirmed:** it is `~/.omnigent/auth_tokens.json` (`cli_auth.py:29`,
   `_TOKEN_FILE_NAME = "auth_tokens.json"`). The `~/.omnigent/n` strings are a doc-rendering
   redaction artifact, not a real path. The briefing's `auth_tokens.json` is correct.

2. **The native-hook fail-closed bug is FIXED on main, not open.** Any claim that the native
   PreToolUse hook "may not refresh → 401 → fail-closed after ~1h" is **stale for in-scope
   harnesses**. `native_policy_hook.py:133-168,500-522` adds `policy_hook_reauth` + the 401/302
   re-mint, and claude (`:657,729,881`) / codex (`:170`) both pass `reauth=`. The bug survives ONLY
   on pi_native (Node), which is out of the claude/codex/Polly scope. Mark §2.G's fail-closed claim
   as fixed-except-pi.

3. **Databricks refresh is per-request, NOT a snapshot.** If §2.G describes the LLM bearer as read
   once and cached, that's wrong: `_DatabricksBearerAuth.auth_flow` re-authenticates on **every**
   HTTP request (`databricks_executor.py:367`); the cheapness comes from the SDK's in-memory token
   cache, not from snapshotting. Likewise `_RunnerDatabricksAuth` is per-request with a 401/302
   retry (`_entry.py:192`). The only true one-shot snapshots are (a) the *baked* native-hook launch
   token (now re-mintable) and (b) the WS-tunnel header (re-minted per reconnect).

---

## 8. Observability & how to read traces

### Observability / Distributed Tracing (and how this doc was verified)

> Authored by the orchestrator from `designs/OBSERVABILITY.md` + hands-on validation against a
> live local stack + Jaeger (PR #1617, merged to `main`). Anchors verified live.

#### Role & boundaries
The tracing layer (`omnigent/runtime/telemetry.py`) gives **one connected trace per user action**
across every process, exported via **OTLP** to any backend (Jaeger locally). It records *structure*
(who called whom, over what transport, how long, decision) — **not** correctness, and **not** the
durable transcript (that's the conversation store; a complementary append-only event log is §9 of
the design, deferred). Opt-in: nothing instruments unless `OMNIGENT_TELEMETRY_ENABLED` is truthy.

#### How it works (the model)
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

#### The five transport techniques (how the trace crosses each boundary)
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

#### Config (env)
`OMNIGENT_TELEMETRY_ENABLED=true` (master opt-in) · `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`
· `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true` · `OMNIGENT_OTEL_CAPTURE_CONTENT=true` (dev only —
records redacted, 4096-char-capped message bodies on host frames / session-updates / policy.content;
keys matching token/secret/password/authorization/credential/api_key → `[redacted]`).

#### How THIS analysis was produced (reproducible recipe)
1. `docker run --rm -d --name jaeger -p 16686:16686 -p 4317:4317 -p 4318:4318 jaegertracing/all-in-one:latest`
2. Telemetry-enabled server on an isolated DB (the installed uv-tool `omnigent` is STALE — no OTel
   deps; run from the worktree `.venv`):
   `OMNIGENT_TELEMETRY_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true OMNIGENT_OTEL_CAPTURE_CONTENT=true .venv/bin/omni server --port 7777 --no-open --database-uri sqlite:///<scratch>/chat.db --artifact-location <scratch>/artifacts`
3. Drive a turn (same env; local runner tunnels to the server, both export):
   `.venv/bin/omni run <bundle-dir> -p "…" --server http://127.0.0.1:7777`
4. Extract by conv id with `scratchpad/jaeger_query.py` (groups by `session.id` across services):
   `summary <conv>` (services + **inter-component edges** + ops + captured payloads), `conv <conv> --denoise` (span tree), `recent <service>`.

#### Reading-the-traces caveats (learned live)
- **Volume:** one trivial turn = ~14 traces / ~1300 spans, dominated by sqlite `connect`/`PRAGMA`
  and ASGI `http send`/`receive`. Always denoise to the inter-component + policy/tool/harness/frame
  spans (the `jaeger_query.py` `summary`/`--denoise` views do this).
- **One conv ≠ one trace.** Don't search by trace_id; search by the `session.id` tag.
- The **"inter-component edges"** view (parent_service→child_service : op) is the architecture
  signal — it's the empirical message catalog per component pair.

#### CUJ corpus generated (evidence used throughout this doc)
`scratchpad/corpus/manifest.tsv` — convs for: claude-sdk turn (`conv_32db…`), claude-native turn
(`conv_94e6…`), subagent-spawn/MCP (`conv_fc47…`), policy-guarded (`conv_eb24…`), resume→new-runner
(`conv_32db…`), fork (`conv_151ad…`, server-only), webui-endpoint battery. codex sdk+native pending
a Databricks `oss` OAuth refresh (see Creds section) — codex behavior is code-verified meanwhile.

---

## 9. Trace corpus index

Real turns driven against a local telemetry server → Jaeger; artifacts in `scratchpad/corpus/` (summary_/tree_/raw_ per conv). Query any conv with `python3 scratchpad/jaeger_query.py summary <conv>`.

| Scenario | Harness | conv id |
|---|---|---|
| sdk-tools | claude-sdk | `conv_32db3f5927d9459fa028cbe69d4173d3` |
| native-tools | claude-native | `conv_94e6e73628564d289e63ab2e61b4d348` |
| subagent | claude-sdk | `conv_fc47380ccbff481abf452a446ec4e40d` |
| policy-guard | claude-sdk | `conv_eb24faf0db784405b0cf3b24891ab2a1` |
| resume | claude-sdk | `conv_32db3f5927d9459fa028cbe69d4173d3` |
| fork | claude-sdk | `conv_151ad0d7431c4ea5a851d1c5b788798e` |
| webui-endpoints | server | `conv_32db3f5927d9459fa028cbe69d4173d3` |
| codex-sdk | codex | `conv_2b4d5e6cc9714c5496464cd87967c4ad` |
| codex-native | codex-native | `conv_a6b880e51d8146ed8a3442b074e75968` |

---

## 10. Round-2 live-driving verification (2026-06-30)

The CUJs that round-1 covered **code-only** (no live trace) were then DRIVEN live against the local
telemetry server (`:7777` → Jaeger) by 5 parallel driver+analyzer subagents, each capturing traces
by conv id and diffing the observed behavior against `ARCHITECTURE.md` / `CUJ-ANALYSIS.md`. Result:
**2 doc-overturning corrections (both re-verified in code), 8 smaller corrections/additions, and a
broad CONFIRM set** that validates the round-1 analysis. 20-conv corpus in `scratchpad/corpus2/`
(per-conv `summary_<conv>.txt`/`tree_<conv>.txt`; query via `jaeger_query.py summary <conv>`).

### ⭐ Flagship corrections — the documented claim was WRONG

**R1. Timers WORK — `sys_timer_*` is functional.** (conv_777fc4b2)
- *Doc said:* ARCHITECTURE §6 + §7-Tools + CUJ-ANALYSIS §2.C/§7 — `sys_timer_*` is
  `NotImplementedError`/non-functional (citing `tools/builtins/timer.py:220`).
- *Live + code:* the runner intercepts `sys_timer_set`/`sys_timer_cancel` at
  `runner/tool_dispatch.py:4133` → `_execute_timer_set` (`:2345`) → a real asyncio `_timer_loop`
  (`:2404`); it returns `{"status":"scheduled"}` and the timer **fired mid-turn**
  (`[System: timer … fired]`). `_TIMER_TOOLS` is a frozenset at `:263`. The `timer.py:208-220` stub
  (`_spawn_timer_workflow`→`NotImplementedError`) is **dead code on the runner path** — round-1
  cited the stub and never executed the tool. ⇒ flip every "timers non-functional" claim.

**R2. Hard affinity has runner FAILOVER — "no failover" is WRONG.** (conv_33219e7c)
- *Doc said:* ARCHITECTURE §6 invariant + §7-Runner — an offline bound runner → `RUNNER_UNAVAILABLE`,
  no failover/recovery.
- *Live + code:* two distinct paths. The **message path** (`POST /events`) treats a dead runner on
  a *live host* as recoverable — "host-relaunch optimism" (`server/app.py:1590-1601`): persist the
  input, send `host.launch_runner` (`_launch_runner_on_host` `sessions.py:6035`), **LWW-rebind**
  (`replace_runner_id`), return `{queued:true}`, and the turn runs. `RUNNER_UNAVAILABLE` (503,
  `runner/routing.py:175` `client_for_existing_conversation`) is raised ONLY on the **resource
  path** (`GET /resources/*`, which doesn't relaunch) and when the **host itself** is dead. Affinity
  (one conv ↔ one `runner_id`, no rebalancing) holds; "no recovery" does not. Round-1 read the
  routing path and over-generalized.

### Corrections (DIFFERENT)

**R3. `sys_add_policy` is gated by a hidden ASK.** (conv_5b5ba2e4) `policies/builtins/safety.py:189
ask_on_add_policy` ASKs before every `sys_add_policy` (builder-injected, NOT shown in
`GET /policies`). Headless → auto-decline → **no policy created**, never reaches `POST /policies`.
CUJ-ANALYSIS §2.D "activates immediately" is wrong — creation is itself an approval-gated action.

**R4. switch-agent changes harness via the agent clone, not `harness_override`.** (conv_ecd393→52e6,
conv_622a68) The route deletes+re-clones the target as a session-scoped agent (new `agent_id`, name
"X (switch …)"), leaves `harness_override` NULL; the effective harness is derived from the cloned
agent's spec by `_resolve_harness` (`sessions.py:2718`). Target must be a **built-in/template**
(`agent.session_id is None`) — a bundle-run (session-scoped) agent 404s "not bindable". 409 if
running. Switch-back is **conditional**: `omnigent.switch.previous_builtin_id` is recorded only when
leaving a built-in (or its clone), so leaving a bundled agent has no switch-back target.

**R5. `sys_os_shell` ignores `os_env.cwd`.** (conv_85f506c5) It resolves cwd via
`create_os_environment` (`spec.cwd or os.getcwd()`), NOT the `_resolve_cwd` precedence chain the docs
cite — that chain is `sys_terminal_*`-only. Scope the cwd-precedence claim to terminals.

**R6. Param fixes.** Archived sessions are listed with `?include_archived=true` (not
`?archived=true`); archive requires `LEVEL_OWNER`. (conv_e7ca5eb1)

### Additions (NEW — not in the docs)

**R7. Policy-enforcement location follows the tool's dispatch route.** (conv_398cb8d7, conv_c8fa7569)
`RunnerToolPolicyGate` (`runner/policy.py`) enforces **function**-type tool_call/tool_result policies
locally for **locally-dispatched builtins** (fast ALLOW, escalate ASK to the server). But tools
routed through the server `/mcp` gate (custom MCP, `sys_os_*`) are policy-gated **server-side at
`/mcp`** — so the live `read_only_os` DENY on `sys_os_write` arrived on the `POST /mcp` edge, not
locally. `label`/`prompt` policies are always server-side (`runner/policy.py:13`). ⇒ the §6 "runner
fast-path" is route-dependent, not universal.

**R8. Tool dispatch is three-way.** (conv_c8fa7569, conv_777fc4b2) custom-MCP (`server__tool`) +
`sys_os_*` → server `POST /mcp` (gate) + runner `POST /mcp/execute`;
`sys_call_async`/`sys_read_inbox`/`sys_timer_*` → **local runner builtins, NO `/mcp/execute` edge**.
Refines §4's `__`-namespace model.

**R9. Headless ASK always auto-declines (SDK).** (conv_3f58b08a) An SDK ASK is a runner-parked Future
that **never populates `pending_elicitations`**; a headless client emits an explicit decline in <7s
(not a timeout), and an external `accept` POST can't win the race. The resolve route itself works
(202, push blocked) — but **resolve-to-ACCEPT is a web/native interactive capability, not
headless-reproducible**.

**R10. `omni run --resume` cannot drive a turn headlessly** (confirmed by 2 agents). It drops to the
interactive agent-picker → `Abort` (bundled/codex/native) or detaches ("not a terminal", native).
The working multi-turn driver is `POST /events` on the re-bound runner — which drives **SDK** turns
but **NOT native** (the message persists, the native runner stays idle without a vendor loop;
persist-before-forward still holds). A clean runtime SDK↔native separator. (Caveat: this means the
round-2 "mid-session effort" self-test's 2nd turn via `--resume` was a no-op picker-abort; the real
multi-turn evidence is the `POST /events` model run, conv_4cd033be.)

**R11. Local runners never self-expire.** All pooled local runners stay `online:true` (the host holds
the WS tunnel; no reaper) — forcing the offline/failover path requires SIGKILL of the bound
`runner._entry` PID. **R12.** Compaction is idle-only (409 mid-turn) and needs a configured
summarizer model (400 otherwise).

### CONFIRMED as documented (validates round-1)
custom stdio MCP (namespaced `mcpsrv__magic_word`, gate+execute) · async inbox · OSEnvironment one
ABC · mid-session **model** propagation (`claude-sonnet-4-6`, live `set_model`) · mid-session
**effort** (None→high PATCH) · DENY 5-phase + short-circuit (`read_only_os` blocks the write) · ASK
held-Future + **no keystrokes** · interrupt fencing (turn truncated mid-word + synthetic
`[System: interrupted]` item; `session.interrupted` SSE-only) · SSE reconnect = heartbeat-first
snapshot, no replay buffer, `[DONE]` on exit · native resume = history-rebuild + forwarder pattern
(inject ×1 vs forwarder `POST /events` ×9-16, `/labels` ×7-10) · plain resume **preserves**
`external_session_id` (switch **clears** it — asymmetry) · codex mid-session model persists (thread
rebuild is code-only, no distinct span).
