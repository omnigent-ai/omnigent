> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.
>
> **⚠️ CORRECTION (round-2 live-driving):** `sys_timer_*` **DO work** — the runner intercepts at `runner/tool_dispatch.py:4133` → `_execute_timer_set` → a real asyncio `_timer_loop` (returns `scheduled`, fires mid-turn). The `timer.py:220` `NotImplementedError` stub is **dead code on the runner path**. Every "timers non-functional / silently dead" statement below is **superseded** (../ARCHITECTURE.md §10 R1). Also: tool dispatch is **three-way** — custom-MCP + `sys_os_*` route through the server `/mcp` gate, but `sys_call_async`/`sys_read_inbox`/`sys_timer_*` are **local runner builtins** (no `/mcp/execute` edge); and `sys_os_shell` ignores `os_env.cwd` (the `_resolve_cwd` precedence is `sys_terminal_*`-only).

# Tools / MCP / Sandbox (OmniBox)

Scope: tool registry + `sys_*` surface, MCP routing (Omnigent-MCP vs custom vs native relay
vs serve-mcp), shells/terminals/cwd, the OS sandbox (filesystem/egress/credential), timers,
system resources. Harnesses in scope: claude-sdk, claude-native, codex, codex-native, Polly.

All `path:line` below were opened and confirmed against the `master-arch-docs` worktree
(main + #1617). Anything I could not confirm is tagged `(unverified)`.

---

## 1. Role & boundaries

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

## 2. Key files & entrypoints (verified)

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

## 3. Internal model

### Tool registry (`ToolManager`)
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

### Custom-MCP pool — two interchangeable managers (same interface)
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

### `ServerMcpPool` (`server/mcp_pool.py:105`) — vestigial
Keyed by `agent_id`, warm-on-demand, LRU. **NOT used by the `/mcp` route** — the handler
delegates execution to the runner (`sessions.py:13605` param is `# ARG001 retained for API
compat`, docstring `:13638` "Unused"). See §7.

### OS sandbox model (`SandboxPolicy`, `sandbox.py:49`)
Resolved policy serialized parent→helper. Fields: `read_roots`/`write_roots`/`write_files`
(filesystem view), `allow_network`, `egress_relay_port`/`egress_socket_path`
(default-deny relay), `deny_unix_socket_paths` (block reach-back to control sockets),
`cwd_allow_hidden` (dotfile masking), `spawn_env_allowlist`/`env_passthrough` (env pruning).
**`credential_proxy` is deliberately NOT serialized** (`:151-157`) — only synthetic
placeholders cross into the helper; real secrets never touch the policy that logs/dumps.
Backends: `linux_bwrap`, `darwin_seatbelt` (both **spawn-time**: wrap argv with launcher),
`none`. `_SPAWN_WRAP_BACKENDS` (`:41`).

### OSEnvironment
ABC `os_env.py:263`; the **only** concrete impl is `CallerProcessOSEnvironment` (`:778`).
`create_os_environment` (`:883`) raises `NotImplementedError` for any `spec.type !=
"caller_process"` (`:887`). `fork` and `sandbox` are **attributes/modes** of that one env, not
distinct env types: `fork=true` copy-trees cwd into `omnigent-fork-*/root` (`:892-896`);
`sandbox` is the resolved `SandboxPolicy` applied to each `shell()` helper subprocess. (Correction in §8.)

### Terminal
`TerminalInstance` (`terminal.py:718`) = one command in its own private tmux server
(isolated socket). Keyed `(conversation_id, terminal_name, session_key)`. Lives in the
AP-process `TerminalRegistry` so panes survive across turns within a conversation
(`manager.py:583`).

---

## 4. Inter-component channels

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

## 5. CUJ behaviors

### A tool call, per harness (the routing answer)
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

### Custom user MCP (`tools.mcp` / `tools/mcp/<name>.yaml`)
Spec `MCPServerConfig` (`types.py:845`): `transport: http|stdio` (default `http`). `http` →
SSE client (`url`,`headers`,`databricks_profile` OAuth). `stdio` → local subprocess
(`command`,`args`,`env`) — **runs unsandboxed** (`types.py:860`; the old `srt` wrap was removed
because its default network-deny silently hung MCPs). Per-server `tools:` allowlist filters at
registration (`:933`). Pooled by `RunnerMcpManager` keyed on `compute_spec_hash`. In AP mode
the call still flows runner→server `/mcp`→runner `/mcp/execute`→`RunnerMcpManager`→subprocess.

### ASK / elicitation round-trip (MRTR)
On TOOL_CALL=ASK the server returns an MCP `InputRequiredResult` (`_mcp_input_required_response`,
`sessions.py:12989`) + emits an SSE `response.elicitation_request`. The runner's
`ProxyMcpManager.call_tool` parks on `pending_approvals.wait_for_user_approval` and retries once
with `inputResponses` (`proxy_mcp_manager.py:257-300`). Retry id MUST differ (`id:2`, `:289`).
**Server re-evaluates the policy on retry** (`sessions.py:13239`) — a forged/unsigned
`requestState` can't bypass a DENY (fail-closed). External-MCP elicitations bubble the same way
(`sessions.py:13434` `input_required` → SSE → retry on runner). So **elicitation responses get
back via the approval Future + SSE-driven UI, NOT keystrokes** (keystrokes are only the
native *web-input* path via tmux send-keys, a separate channel).

### Shells exposed to agents
- `sys_os_shell` (`os_env.py:334`): one-shot `command` → `OSEnvironment.shell()` (async,
  `:363`); returns `{stdout,stderr,exit_code}`. Gated on `os_env:` in spec.
- `sys_terminal_*` (`sys_terminal.py`): persistent **tmux panes** for interactive REPLs; gated
  on `terminals:` block. `launch/send/read/list/close`. This is also how native harnesses get
  their own TUI pane (the runner auto-creates a `claude/main` terminal).

### OmniBox (the 3 layers) for one sandboxed shell
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

## 6. Answers to the doc questions (terse, code-anchored)

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

## 7. Reliability gaps / sharp edges (confirmed in code)

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

## 8. Corrections to CUJ-ANALYSIS §2.C

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
