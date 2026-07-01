# CUJ Answers — Tools / MCP / Shells / Sandbox / Timers

Domain: tool surface + custom MCP + OmniBox sandbox + shells/terminals + timers.
Companion architecture doc: `../architecture/tools-omnibox.md`. Every claim is
`file:line`-anchored; the `sys_os_shell` flow is also trace-confirmed.

---

## Q1. The Omnigent `sys_*` surface — tool groups + gating

All builtins register in `omnigent/tools/manager.py` (`ToolManager.__init__` `:105`). Groups
and their gates:

| Group | Tools | Gate | Anchor |
|---|---|---|---|
| File/shell | `sys_os_read/write/edit/shell` | spec `os_env:` set (or pre-resolved env) | `manager.py:525` |
| Terminals | `sys_terminal_launch/send/read/list/close` | spec non-empty `terminals:` | `manager.py:563` |
| Async/inbox | `sys_call_async`, `sys_read_inbox`, `sys_cancel_async` | `async_enabled` (default **True**; `async:false` kills) | `manager.py:200`, `spec/types.py:1537` |
| Task lifecycle | `sys_cancel_task` | **always** | `manager.py:490` |
| Timers | `sys_timer_set`, `sys_timer_cancel` | `timers:true` (default **False**) | `manager.py:231` |
| Sub-agents (read) | `sys_session_list/get_history/get_info` | **always** | `manager.py:427` |
| Sub-agents (write) | `sys_session_send/close`, `sys_list_models`, `sys_advise_models` | `tools.agents` OR `spawn:true` | `manager.py:446` |
| Sub-agents (create) | `sys_session_create` | `spawn:true` only | `manager.py:468` |
| Sub-agents (share) | `sys_session_share` | `agent_session_sharing` (`none`/`non-public`/`public`) | `manager.py:441` |
| Agents | `sys_agent_get/download/list` | **always** (auth-gated server-side) | `manager.py:471` |
| Models | `sys_list_models` | with dispatch grant | `manager.py:457` |
| Policy | `sys_add_policy`, `sys_policy_registry` | **always** | `manager.py:186` |
| Comments | `list_comments`, `update_comment` | **always** (cannot be spec-declared) | `manager.py:511` |
| Skills/web/files | `load_skill`(+`read_skill_file`), `web_search`, `web_fetch`, `upload_file`, `download_file`, `list_files`, `search_conversations`, `export_agent` | `load_skill` always; rest via `tools.builtins` | `manager.py:266`/`:288` |

Important: the opt-in flags control **advertisement**, not authority — spawn writes are
child-only and `sys_session_share` is owner-authority-bounded, both enforced at the server/dispatch
layer regardless (`manager.py:420`). MCP tools are **not** registered here — they're runner-owned.

---

## Q2. Custom (user-defined) MCP servers — declaration, transport, allowlist, pooling

- **Declaration**: `tools.mcp` in agent YAML → `MCPServerConfig` (`spec/types.py:~847`).
  - `transport: "http"` (default) → SSE (`mcp.client.sse.sse_client`); `url`, `headers`.
  - `transport: "stdio"` → subprocess (`mcp.client.stdio.stdio_client`); `command`, `args`, `env`.
    Validator rejects HTTP fields on stdio and vice-versa.
  - Per-server **allowlist** of bare tool names (filtered in `_mcp_tool_schema` `mcp_manager.py:129`);
    per-tool `timeout`/`retry` (inherit `tools.timeout`/`tools.retry`).
- **Pooling** (`RunnerMcpManager`, `runner/mcp_manager.py`): lazy connect; **8-entry LRU**
  (`_POOL_SPEC_CAPACITY = 8` `:45`) keyed by **spec hash** (`compute_spec_hash` over
  `spec.mcp_servers` + stdio cwd `:74`); `_touch` `:470` / `_evict_if_needed` `:476` (head pops,
  connections closed async). Tools namespaced **`{server}__{tool}`** (`:139`) so collisions across
  servers are impossible.
- **Inline approval**: a custom MCP can request `elicitation/create` mid-call → `mcp_elicitation`
  event → web approval card → resolved/declined (`_build_elicitation_callback` `:182`; declined
  when no server client `:218`).

> Cross-ref: the **RUNNER SME** owns the routing/pool-lifecycle mechanism; this answer owns the
> *definition shape* (transport, allowlist, namespacing) + the LRU facts.

---

## Q3. MCP routing — who routes a call where? (custom MCP vs `sys_*`)

**Both** kinds use one seam; the **server** is the policy authority, the **runner** is the executor.

1. Harness emits a tool call:
   - **SDK harnesses** (claude-sdk/codex/Polly): the SDK invokes the MCP tool in-process.
   - **Native harnesses** (claude-native/codex-native): the vendor CLI POSTs to a localhost bridge
     relay (Bearer auth) → `_relay_tool_executor` (`runner/app.py:13086`).
2. Runner `ProxyMcpManager.call_tool` (`proxy_mcp_manager.py:178`) POSTs to **server**
   `POST /v1/sessions/{id}/mcp`.
3. Server evaluates **TOOL_CALL** policy (`_evaluate_tool_call_policy`, `server/routes/sessions.py:1077`).
4. On ALLOW, server POSTs **runner** `POST /v1/sessions/{id}/mcp/execute` (`app.py:17829`). The
   runner dispatches **uniformly**:
   - **bare names** (`sys_os_*`, `sys_terminal_*`, async, etc.) → `tool_dispatch.execute_tool`
     (`runner/tool_dispatch.py:10`) using the session's terminal registry / inbox / **runner cwd**.
   - **namespaced `server__tool`** → `RunnerMcpManager` (live stdio/http connections).
5. Result flows back; server evaluates **TOOL_RESULT** policy, returns up through `/mcp`.

So: **custom-MCP tools and `sys_*` tools take the *same* server-gated path**; the only difference
is the final dispatch fork inside `/mcp/execute` (RunnerMcpManager vs runner-local executor). The
runner also has a fast-path ALLOW/DENY before dispatch; ASK escalates to the server.

**Out-of-turn exception**: native bridges additionally run a separate `serve-mcp` MCP stdio server
(in the vendor CLI's `settings.json`) exposing **only `sys_os_*`** against the workspace cwd, **no
sandbox** (`claude_native_bridge.py:3116`) — the vendor CLI's file access *between* Omnigent turns.
(`serve-mcp` is the native bridge's argparse subcommand, **not** a top-level `omnigent` command —
corrects CUJ-ANALYSIS §2.C "exposed via serve-mcp subcommand".)

---

## Q4. Shells — creation, cwd resolution, the two paths, orphan reaping

**Two ways shells reach agents:**
1. **`sys_os_shell`** — one-shot command in the agent's **shared `OSEnvironment`**
   (`os_env.shell()` `os_env.py:294`). All four `sys_os_*` share **one** `OSEnvironment` instance
   so cwd/sandbox/env stay consistent across calls (`build_os_env_tools` `os_env.py:378`).
2. **`sys_terminal_*`** — **persistent named tmux panes** (`TerminalRegistry.launch`
   `terminals/registry.py:160`), keyed `(conversation_id, terminal_name, session_key)`, surviving
   across turns; tmux `remain-on-exit on` keeps dead panes for inspection (`terminal.py:156`).

**Working-directory resolution precedence** (`_resolve_cwd`, `sys_terminal.py:752`, first match wins):
1. **LLM `cwd_override`** (vetted vs `allow_cwd_override`).
2. **`terminal.os_env.cwd`** (skip if `None`/`""`/`"."`/`"./"` or the `"inherit"` sentinel).
3. **`spec.os_env.cwd`** (same meaningful-cwd test).
4. **`ctx.workspace`** (per-task workspace from `runtime/workflow.py`).
5. *(implicit)* if all miss → returns `None` → caller falls back to **host/runner cwd**.

Trace confirms tier 4/5: the `sys_os_shell` TOOL_RESULT carried
`cwd=/Users/.../omnigent-worktrees/traces` (the runner workspace).

**Reaping (two reapers):**
- **Orphan reaper** (`reap_orphaned_terminals` `terminal.py:581`) at **runner startup**: each
  instance dir records its **owner pid**; the sweep `tmux kill-server`s every instance whose owner
  pid is gone and removes the dir (markerless dirs left alone). Fixes the one-tmux-server-per-session
  leak from SIGKILL'd runners.
- **Idle pane reaper** (`pane_reaper.py`, #1349): background loop reaping **native** panes after a
  **30-min** idle window (`OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S`, `0` disables), 3-signal busy check
  with a second pre-teardown re-check; pane-scoped teardown.

---

## Q5. OmniBox — the OS sandbox (3 backends × 3 isolation layers)

**OmniBox = the OS-level sandbox**, not a web component. Resolution in `inner/sandbox.py`
(`resolve_sandbox` `:371`, `_default_sandbox_for_platform` `:806`).

**`OSEnvironment` modes**: `caller_process` (no isolation, in-process) · `fork` (workspace copy) ·
`sandbox` (one of the backends below).

**Sandbox backends:**
- **`linux_bwrap`** — bubblewrap: mount/PID/UTS/IPC namespaces (`--unshare-*`), `--ro-bind-try`
  read roots, `--proc`/`--dev`, **tmpfs-masked dotfiles** in cwd (unless `cwd_allow_hidden`;
  `.venv` default), **hardened seccomp denylist**.
- **`darwin_seatbelt`** — `sandbox-exec -f <profile>` SBPL, `(deny default)` baseline + selective
  allows; **no namespaces, no seccomp** (capability-level); masking is access-deny (not invisible);
  deny-wins.
- **`windows_jobobject`** — Job Object process-tree containment + resource limits; **no FS/network
  isolation**.
- **`none`** — the only explicit opt-out.
Fail-loud: a missing backend binary errors at sandbox **build** (not silently unsandboxed).

**The 3 isolation layers:**
1. **Filesystem isolation** — only granted paths visible (bwrap binds; seatbelt deny-default);
   dotfiles masked.
2. **Default-deny L7 egress proxy** (`inner/egress/`):
   - DSL rules `METHODS host/glob`, default deny (`rules.py:185`).
   - DNS-safe host allowlist `[A-Za-z0-9.-]` (`rules.py:49`) → kills NUL/percent/CRLF smuggling
     (cites the Claude Code sandbox-runtime CVE class).
   - **Private-destination block** (default on): resolve-**once** then refuse any non-global IP —
     RFC1918/loopback/link-local/ULA/reserved/TEST-NET, **CGNAT 100.64**, multicast, and
     **cloud-metadata traps** (`169.254.169.254`, Azure `168.63.129.16`) (`proxy.py:124`/`:914`).
     Resolve-once defeats DNS rebinding.
   - MITM with per-sandbox CA; in-ns relay → parent proxy over a Unix socket (the *only* egress path).
3. **Credential injection** (`inner/credential_proxy.py` + `egress/proxy.py`):
   - **Swap-on-access (default)**: nothing credential-shaped in the sandbox; tool sends no
     `Authorization`, proxy injects `<scheme> <real>` for the bound host (`proxy.py:1145`).
   - **Opt-in placeholder**: parent injects a single-use `oa_cred_*` token; client sends it; proxy
     swaps to real (`proxy.py:1129`).
   - **Leak guard**: wrong-host/unknown `oa_cred_*` → **403** (`proxy.py:1135`); a real client-set
     `Authorization` is forwarded untouched (no clobber `:1125`).
   - Real secret lives only in the parent + proxy table; **never** serialized into `SandboxPolicy`
     (`sandbox.py:151`), argv, or sandbox disk.

---

## Q6. Timers + Async/Inbox (definitions)

- **Timers** (gated `timers:true`, default False): `sys_timer_set` returns a `timer_id`
  **synchronously** (`timer.py:179`); the firing later arrives as `[System: timer X fired]` via the
  `async_work_complete` drain with `kind="timer"` (`manager.py:170`). ⚠️ **sessions-native firing
  path is `NotImplementedError`** (`timer.py:220`); `sys_timer_cancel` returns no-active-timer.
- **Async/inbox** (gated `async_enabled`, default True): `sys_call_async` fire-and-forget;
  results drain via the `async_work_complete` inbox — auto-collected each loop iteration
  (`_drain_async_completions`, RUNTIME-owned) or pulled by `sys_read_inbox` (`async_inbox.py:240`);
  `sys_cancel_async` aliases `sys_cancel_task`. (RUNTIME SME owns the drain mechanics.)

---

## Trace evidence (the load-bearing case)

Corpus `conv_63542a5f92e24956812e19b104eac0e9` (`sys_os_shell` running `echo TRACETEST123`):

**Trace `cfb59197f6f9`** (the `POST /events` trace) — the MCP + policy chain:
```
omni-server policy.evaluate REQUEST      ALLOW
omni-server policy.evaluate LLM_REQUEST  ALLOW
omni-server POST /v1/sessions/{id}/mcp           ← omni-runner:POST          (runner → server)
omni-server policy.evaluate TOOL_CALL    ALLOW tool=sys_os_shell  ← /mcp     (THE GATE)
omni-runner POST /v1/sessions/{id}/mcp/execute   ← omni-server:POST          (server → runner exec)
omni-server policy.evaluate TOOL_RESULT  ALLOW tool=sys_os_shell  ← /mcp
omni-server policy.evaluate LLM_RESPONSE ALLOW
```
- TOOL_CALL `policy.content` = `{"name":"sys_os_shell","arguments":{"command":"echo TRACETEST123"}}`.
- TOOL_RESULT `policy.content` carried `"cwd":"/Users/.../traces"` → confirms cwd tier 4/5 (runner workspace).

**Trace `f98feda729f6`** (harness OpenInference tree):
```
omni-harness agent:claude-sdk
omni-harness   tool:sys_os_shell  (openinference.span.kind=TOOL, tool.name=sys_os_shell)
```

**Also**: `omni-runner GET /v1/sessions/{id}/resources/terminals` (traces `906c…`/`7c4b…`) — the
runner probing the **terminal registry** for the session (confirms `sys_terminal_*` registry is live
even on a non-terminal turn).

Mechanism confirmed by **both** code and trace: the runner→server `/mcp` gate, the four policy
phases, the server→runner `/mcp/execute` exec, and the OpenInference `tool:sys_os_shell` span.

---

## Per-harness deltas (in-scope only)

- **claude-sdk**: tools in-process; `tool:` span on omni-harness; all gating via `ToolManager(spec)`.
  Sandboxed claude-sdk on macOS **crashes** vs degrading (#517 part-1, no PR).
- **claude-native**: vendor CLI → bridge relay → same server gate; out-of-turn `serve-mcp` exposes
  only `sys_os_*` (no sandbox).
- **codex / codex-native**: structurally identical to claude (SDK and native respectively) —
  **no live trace** (AI-gateway creds expired, 403).
- **polly** (custom agents): runs on a host harness (typically claude-sdk) and **inherits its tool
  behavior**; `ToolManager` registers from the custom agent's own spec (`os_env`/`terminals`/`tools.mcp`).

---

## Failure branches & gaps

- macOS sandboxed claude-sdk **crashes** (#517 part-1, no PR).
- `credential_proxy` SECURITY: parent-side `subprocess.run(shell=True)` + arbitrary file reads on a
  trusted-spec assumption (`credential_proxy.py:190`, #1542, no PR).
- **Timers** unusable on sessions-native (`NotImplementedError`).
- Egress: non-global resolution → block/403; DNS rebind defeated by resolve-once; IPv6 literals +
  Unicode IDNs rejected (documented limit).
- MCP pool: 9th distinct spec evicts LRU head (connections closed async); inline elicitation declined
  when no server client wired.
- Terminal launch: tmux missing / pane exits early → `RuntimeError` (wrapped as JSON error);
  concurrent same-key launch → second-arrival wins.

## Cross-references
- **RUNNER SME**: `RunnerMcpManager` pool/prewarm, outbound `tools/call`, `/mcp/execute` internals,
  `runner/policy.py` fast-path.
- **RUNTIME SME**: `async_work_complete` drain, inbox queue, timer firing task (when implemented).
- **POLICY SME**: `_evaluate_tool_call_policy`, elicitation registry, fail-closed/open phase set.
