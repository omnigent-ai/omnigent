# "How does claude-code / codex / polly behave when…" — answers

> Paste-ready answers to the **Stability & Reliability** doc's question list. Trace-backed
> (2026-06-30, all four harnesses driven live → Jaeger) and code-anchored. Depth + diagrams live in
> [`ARCHITECTURE.md`](./ARCHITECTURE.md); drift corrections in [`CUJ-ANALYSIS.md`](./CUJ-ANALYSIS.md) §7.
> **Family rule:** behavior splits by **SDK** (`claude-sdk`, `codex`; **Polly** runs here, typically
> claude-sdk, and inherits it) vs **native** (`claude-native`, `codex-native`). `S` = `server/routes/sessions.py`.

## Disconnects
Server↔client SSE is **live-tail only, no replay buffer**: the stream ends with `[DONE]` on every exit path; on reconnect the client re-runs **snapshot + live tail** (open `GET …/stream` first, then read the snapshot, dedup overlap). Heartbeats (15s) detect half-open sockets; the Apps front door caps SSE at ~5min so the client proactively reconnects. The **conversation keeps running** while the client is gone (server is source of truth). The runner↔server WS tunnel is separate: runner pings 30s / dead after 3 misses, then reconnects (re-minting auth). ⚠️ If the bound runner goes offline mid-turn, an offline runner on a **live host** is relaunched on the next message (host-relaunch optimism + LWW rebind, see ARCHITECTURE §10 R2) so the turn proceeds; only if the **host itself** is dead is the conversation stranded — a message persists but isn't forwarded → client stuck "working" until timeout. Same for all harnesses (it's server-side).

## Forking a session
`POST /v1/sessions/{src}/fork` → a **synchronous, server-only deep copy** (`fork_conversation`): a **new top-level conversation**, items copied with **fresh ids**, lineage recorded only via the `omnigent.fork.source_id` label (not a parent/root chain). Instance-scoped labels (`bridge_id`, context tokens) are dropped; optional `up_to_response_id` truncates; cross-family harness switch resets the model. **No runner is spawned until the fork runs a turn** (verified: the fork trace had zero inter-component edges). Native targets rebuild the vendor transcript from a `FORK_CARRY_HISTORY` label on first turn. Harness-agnostic.

## Resuming a session — *how much transcript loads into the runner?*
The **server loads none.** Resume re-binds a fresh runner (`PATCH …/{id} {runner_id}` — **last-writer-wins**, not a CAS) and the **new runner pulls the full transcript itself** via paginated `GET …/items` + `GET …/agent/contents`. SDK harnesses replay that history into the in-process loop; **native** harnesses rebuild the vendor store from the mirrored items + `bridge_id`. So "how much loads" = **the entire conversation history, pulled by the runner on demand**, not pushed by the server. (Resume dispatch: `omnigent/resume_dispatch.py`.)

## Credential resolution
**Provider selection (setup):** `omnigent setup` runs ambient detection (`onboarding/ambient.py` scans installed CLIs — Claude.app/Codex/LM Studio), you pick a provider, it's saved to `~/.omnigent/config.yaml` (`providers:`); Databricks picks reuse logins via profile **aliasing**. **Default model/provider resolution chain:** CLI `--model` → spec `executor.model` → provider default → catalog default → per-harness pin (`providers/__init__.py`, e.g. anthropic→`claude-opus-4-8`). **Three credential relationships, each its own refresh:**
- **LLM creds** — Databricks = **per-request** refresh (`_DatabricksBearerAuth.auth_flow`, `databricks_executor.py:367`); api-key/subscription = static. (Live example: codex routes through the Databricks AI Gateway via a `databricks auth token --profile oss` auth command baked into the codex CLI config — an expired `oss` profile → empty token → turn fails; fixed by `databricks auth login --profile oss`.)
- **Runner↔server** — **two cadences**: WS-tunnel Bearer minted per-connection (re-minted on reconnect); HTTP callbacks per-request with a 401-or-Apps-302→`/oidc/` re-mint (`runner/_entry.py:192`).
- **Client↔server** — `__Host-ap_session` cookie (accounts/oidc) or `X-Forwarded-Email` header (proxy mode) or a `Bearer` from `~/.omnigent/auth_tokens.json` (CLI; **no background refresh** — expired → re-login).
- **Policy-hook path (native):** the `/policies/evaluate` hook token now **re-mints on 401/302** (`post_evaluate_with_retry(reauth=…)`) — the old "fail-closed after ~1h" bug is **fixed on `main`** for claude/codex; only `pi_native` (out of scope) remains.

## Request lifecycle
SDK turn (canonical): client `POST /events` → server **persists item before forwarding** (`S:8540` then `S:8697`) → publishes `session.input.consumed` → runner pulls agent+history (`/agent/contents`, `/items`, `/skills`) → executor runs the in-process loop → `policy.evaluate` phases (REQUEST→LLM_REQUEST→LLM_RESPONSE) → `response.*` deltas stream over SSE; **only `OutputItemDoneEvent` persists** → tool calls go runner→server `POST /mcp` (policy gate) → server→runner `POST /mcp/execute` → `TurnComplete` → idle. **Native turn:** same persist step, but the **forwarder is the sole history writer** (single-writer bypass `S:8735`); the runner injects one user message into the vendor CLI and the forwarder posts `external_*` events back (`POST /events` ×N) + reads `/labels`. See ARCHITECTURE.md §3.

## MCP routing
- **Omnigent MCP (`sys_*`):** SDK harnesses get an in-process `create_sdk_mcp_server(name="omnigent")` (`mcp__omnigent__*`); calls route model→runner→ server `POST /mcp` (central policy gate) → runner `POST /mcp/execute`. The `__` namespace split routes `server__tool`→custom MCP vs bare `sys_*`→builtin.
- **Custom (user-defined) MCP:** declared in YAML `tools.mcp` (HTTP/SSE or stdio); pooled by the runner (`ProxyMcpManager` live = routes through the server gate; `RunnerMcpManager` = direct, LRU-8, `{server}__{tool}` namespacing). Can request approval via inline `elicitation/create`.
- **Native:** a long-lived `serve-mcp` stdio child switches on `tool_relay.json` — **in-turn** → localhost-HTTP relay to the harness (**gated**); **out-of-turn** workspace tools → runs `sys_os_*` locally **UNGATED** (⚠️ gap).

## Hooks for elicitations / required hooks / how verdicts return
- **claude-native:** `PreToolUse` + `PermissionRequest`/`UserPromptSubmit` hooks → `POST /policies/evaluate`. **codex-native:** the **same shared** policy hook (`PreToolUse`/`PostToolUse`/`UserPromptSubmit`); its `codex-elicitation-request` endpoint is codex's *own* permission prompt (separate from policy ASK). **SDK (claude-sdk/codex/Polly):** an `approval` event → runner `pending_approvals` Future.
- **Which hooks make ALL policies work (native):** `UserPromptSubmit` (the sole native REQUEST gate), `PreToolUse` (TOOL_CALL), `PostToolUse` (TOOL_RESULT). REQUEST + TOOL_CALL are **fail-CLOSED**.
- **How a verdict returns: NO keystroke emulation** for any in-scope harness. Native verdicts return via **long-poll-held HTTP** (the `/policies/evaluate` response body blocks until ALLOW/DENY); SDK via the `approval` event/Future. ASK → publish `response.elicitation_request` → web ApprovalCard → `POST …/elicitations/{eid}/resolve` (`S:18014`) → resolves the Future → forwards.

## Dedupes (server / runner / client)
**NO server-side dedup** (only `(conversation_id, position)` is unique). Dedup is **client-side** — WebUI by `ctx.itemId` (3 sites in `chatStore.ts`, merge point `pumpStreamEvents:3027`); TUI by **crude counters** (`_pending_local_user_sends`, `_TurnProseTracker`) — **plus runner cold-cache** by `persisted_item_id`. `session.input.consumed` is a *client* anchor, not a server dedup set.

## TUI vs WebUI state
Both are pure server clients (server = source of truth). **WebUI**: SSE per conversation + a **`WS /sessions/updates`** push for the live sidebar + an HTTP `/health` poll. **TUI**: **SSE only — no `WS /sessions/updates`** (no live sidebar; the sub-agent rail is polled). The REPL uses a bespoke single-SSE-pump adapter (`_repl.py:1234`), not the python-client `SessionsChat`. Neither persists anything locally.

## "Working" state — how it's computed & propagated
Server-authoritative and **in-memory** (`_session_status_cache`, single-replica). WebUI chat "Working…" = the store's `sessionStatus` (from the `session.status` SSE event) via `computeIsWorking` in `ChatPage`; the **sidebar badge** is separate (`useSessionState` over the list row, fed by `WS /sessions/updates`). TUI shows a spinner gated on `session.status` running/idle. ⚠️ all live status/presence/fence state desyncs on restart or a second replica.

## Transcript reconstruction
- **Compaction:** `runtime/compaction.py` L1 clear tool-results → L2 LLM summary → L3 truncate; auto on `ContextWindowExceededError`, or user `type=compact`; native posts `external_compaction_status`.
- **Durable vs streaming:** only `OutputItemDoneEvent` persists; `response.*` deltas (incl. **reasoning**), `session.*`, turn-lifecycle are **SSE-only — reasoning is never persisted** (re-derived on SDK, mirrored on native).
- **Local↔server mismatch:** server is canonical; native risk is the forwarder being the single writer (a mirror lag shows as missing items). Fork = fresh-id deep copy; resume = runner re-pulls full history.

## API routes & message formats
Full catalog in ARCHITECTURE.md §4. Summary: **client→server** = REST (`POST /sessions` multipart, `/events`, `/fork`, `PATCH`, `/elicitations/{id}/resolve`, snapshot/items/child_sessions GETs) + **SSE** (`/stream`) + **WS `/sessions/updates`** (web only). **server↔runner** = everything over the **WS reverse-tunnel** (framed HTTP, headers verbatim): server→runner `POST /sessions|/events`, `GET /skills|/resources/terminals`, `POST /mcp/execute`; runner→server `GET /items|/agent/contents|/labels`, `POST /events|/policies/evaluate|/mcp`, `PATCH`. **host↔server** = WS JSON control frames (16 kinds). **runner→harness** = UDS `POST /events` (SDK drives the turn) / one inject (native). Reasoning streams, never persists.

## Harness-specific features (matrix)
See ARCHITECTURE.md §5 for the verified table. Headlines: **interrupt (web Stop)** ✅ all four (claude-sdk via `interrupt()`, codex-sdk via `interrupt_turn()`, claude-native via bridge Escape, codex-native via `turn/interrupt`); **queue** ✅ all, but **only claude-sdk** supports *tool-boundary* interrupt (others apply queued input at turn boundaries); **subagents** ✅ (SDK via `sys_session_*`; native via `external_subagent_start`); **reasoning-effort** ✅ all (claude {low..max}, codex {none..xhigh}); **elicitation** ✅; **mid-session model** — claude-sdk live `set_model`, codex-sdk thread teardown+rebuild, codex-native `thread/settings/update`, **claude-native vendor-only/next-turn (⚠️)**. **Own-config propagation:** claude-native `use_claude_config` passes through `~/.claude/*`; codex inherits `~/.codex/config.toml`.

## Reconciling streaming + durable into one view / close page & return
WebUI holds optimistic `pendingUserMessages` off the `blocks` list until `session.input.consumed`, dedups persisted-vs-streamed by `ctx.itemId`, and stamps the streamed-text block with its durable id in place. **Close & return:** opens the SSE stream **first**, then merges a *slim* snapshot (`getSessionSlim`) + a *windowed* history page — not a full reload. TUI does a one-time `GET /items` replay + live SSE, dedup by counters.

## Role of the executor
The per-turn **event generator** at the bottom of `ExecutorAdapter.run_turn` (`_executor_adapter.py:394`): it translates a vendor SDK/CLI into Omnigent's `ExecutorEvent`s (TextChunk/ReasoningChunk/ToolCallRequest/…/TurnComplete) and declares capabilities. It owns **event translation + backend state + capabilities** — NOT history, policy, SSE, or tool dispatch (the runner does those). Base capabilities default ❌ except `supports_tool_calling` (`executor.py:541`).

## Subagents — spawning & depth
The LLM calls the **generic `sys_session_send`** (not a per-`AgentTool` tool); the child Conversation is minted on the **runner** (`tool_dispatch.py:1146`) via `POST /v1/sessions` under the **parent's** `agent_id`; results drain back via a runner-internal queue + `sys_read_inbox` (consume-once). **Depth: ⚠️ no spawn-time cap** — `_MAX_SUBAGENT_TREE_DEPTH=3` is display-only; `SelfAgentTool` pruning is parse-time only; runtime clone-spawns-clone is possible (runaway risk). Verified live in the subagent trace (`conv_fc47…`).

## Inbox
`sys_call_async` spawns a background task → handle; results auto-drain at the iteration boundary or via `sys_read_inbox` (topic `async_work_complete`, **consume-once**). ⚠️ `sys_cancel_task` is a **no-op** (tasks table dropped).

## OmniBox
The OS sandbox (not a UI). One `OSEnvironment` class with `fork`/`sandbox` **attributes** (`create_os_environment` raises for other "types"). Three layers: bubblewrap+seccomp / Seatbelt **filesystem isolation**, **default-deny egress proxy** (`METHOD host/path` allowlist, private-IP/metadata blocked, host-smuggling-hardened), and **L7 MITM credential injection** (agent holds a placeholder; the proxy swaps the real secret on allowed requests; secrets are parent-only, never serialized).

## WebUI sidebar fetching / full client→server request set
Sidebar = `useConversations` → `GET /v1/sessions` (cursor-paginated 20/page, `?search_query=`), live via `WS /v1/sessions/updates` (watch-set snapshot + changed/removed deltas + heartbeat); projects via `GET /v1/sessions/projects`. The **entire** client→server surface (REST + SSE + WS) is enumerated in ARCHITECTURE.md §4 / §7-Web / §7-TUI.

## Policy enforcement
**Creation:** session-level (`sys_add_policy` → `POST …/policies`, registry-allowlisted), admin-default (`POST /v1/policies`), or spec-declared (immutable). **Server-level vs session/runner:** the **server** is authoritative (default+spec policies, LLM-phase gating, the elicitation registry); the **runner** runs a fast-path ALLOW/DENY (`RunnerToolPolicyGate`) before MCP dispatch and **escalates ASK** to the server. Phases REQUEST/TOOL_CALL/TOOL_RESULT/LLM_REQUEST/LLM_RESPONSE; **REQUEST + TOOL_CALL fail-CLOSED**; DENY short-circuits, ASK accumulates.

## System resources (shells, cwd, timers)
**Shells:** `sys_os_shell` (shared OSEnvironment shell) and `sys_terminal_*` (persistent named tmux panes). **Working dir** precedence (`sys_terminal.py:_resolve_cwd`): LLM override → `terminal.os_env.cwd` → `spec.os_env.cwd` → `ctx.workspace` → runner cwd. Shells reach the agent as tools dispatched through the runner. **Timers: `sys_timer_*` WORK** (the runner intercepts at `tool_dispatch.py:4133` → real asyncio loop; the `timer.py:220` stub is dead code on the runner path — corrected, ARCHITECTURE §10 R1).

## Custom agents — storage, subagent init, harness switching, caching
**Storage (3 tiers):** ArtifactStore (content-addressed tarball = source of truth) → `Agent` DB row (id/name/bundle_location/version/session_id) → server `AgentCache` (in-mem+disk, **no TTL**, evict-on-delete, warm-swap-on-update); the runner keeps a separate `(agent_id,version)` disk cache. **A custom agent's own subagents** init when the runner swaps `spec` to the child sub-spec via `_find_spec_by_name` at child-turn start (`app.py:8721`); inline subagents share the one parent bundle/`agent_id`. **Harness switching:** `POST …/switch-agent` (idle-only, 409 if running); for native targets it **clears `external_session_id`** so the next turn rebuilds. **Caching/refresh:** agent cache no-TTL (invalidate on delete/update); provider catalog 1h TTL; model listing 5min TTL; credentials resolved per-request (no snapshot) except the per-reconnect tunnel header.

## Instrumentation / tracing through components
PR #1617 instruments all of it (Host Daemon, Runners, Web, TUI, Server, in-process Policy, DB) with W3C trace propagation; every span is tagged `session.id`=`conv_…`. See ARCHITECTURE.md §8 for how to run the local stack + read traces, and §4 for the empirical "what data, what channel" catalog between every component pair.
