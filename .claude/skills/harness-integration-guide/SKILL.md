---
name: harness-integration-guide
description: Reference guide for building new Omnigent harness integrations — covers SDK/subprocess harnesses and native harnesses as separate tracks, each with their own feature matrix, implementation patterns, and prioritized checklist.
---

# Harness integration guide

This skill describes the **feature matrix** every Omnigent harness must
consider. Use it when planning, reviewing, or implementing a new harness.

Omnigent has two distinct harness tracks with different architectures and
feature sets:

- **SDK/subprocess harnesses** — run the vendor model directly (in-process SDK,
  CLI subprocess, or ACP subprocess). They own the model lifecycle.
- **Native harnesses** — wrap a vendor's own TUI or server and mirror its
  output into Omnigent. They observe and relay, rather than drive.

---

## Part 1 — SDK / subprocess harnesses

These harnesses run the vendor model directly and bridge Omnigent tools into
the vendor's tool-calling interface.

### Capability matrix

| Capability | What it means |
|---|---|
| **Connects to Omnigent MCP** | Harness exposes/consumes tools via the MCP protocol (in-proc SDK MCP server) |
| **Model override** | User can select a model via `--model` / config; some harnesses are vendor-locked (e.g. Claude-only, GPT-only, Gemini-only) |
| **Auth** | How credentials are obtained — API key, gateway token, vendor CLI login, OAuth, etc. |
| **Streaming** | Harness forwards token-level or delta-level streaming to the Omnigent forwarder |
| **Policies / Elicitation (web)** | How the harness gates tool use — `canUseTool ASK`, `request_permission`, 2-stage cards, pre-tool hooks, or policy DENY |
| **Interrupt** | User can cancel a running turn mid-stream |
| **Live queue (concurrent)** | Multiple turns can be queued and processed concurrently |
| **Tool-boundary steer** | Omnigent can inject steering text at tool-call boundaries |
| **Resume/fork from Omnigent transcript** | Rebuild a conversation from a stored Omnigent transcript (replay history, seed prompt, or vendor session ID) |
| **Compaction** | Long conversations are compacted; harness surfaces `CompactionComplete` events |
| **Reasoning** | Model reasoning/thinking tokens are forwarded |
| **Images** | Image content (screenshots, diagrams) is forwarded — full binary, path reference, or text-flattened |

### Current harness status

| Harness | Implementation | MCP | Model override | Auth | Streaming | Policies | Interrupt | Concurrent | Steer | Resume/fork | Compaction | Reasoning | Images |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-sdk | SDK in-process | yes | yes (Claude-only) | Anthropic key / Databricks gateway | yes | canUseTool ASK | yes | yes | yes | yes | yes (CompactionComplete) | yes | yes |
| codex | CLI subprocess (app-server RPC) | no (dynamicTools RPC) | yes (GPT-only) | Databricks gateway / codex auth.json | yes | canUseTool ASK | yes | yes (turn/steer) | no | yes (full history replay) | no | yes | yes |
| cursor | SDK in-process | no (SDK custom-tool) | ~(Cursor ids only) | Cursor API key | yes | 2-stage + card | yes (close) | no | no | yes (persistent agent replay) | no | yes | yes |
| pi | CLI subprocess (JSONL RPC) | no (TCP socket) | yes (multi-model) | Databricks gateway / API keys | yes | policy + native gate | yes | yes | no | yes (history prefix replay) | no | yes | yes |
| kimi | CLI subprocess (stream-json) | no | yes (-m multi) | kimi config | yes | no (vendor TUI gates) | yes | no | no | no (vendor session id) | no | ? | ~(dropped) |
| qwen | ACP subprocess | no | yes (multi / gateway) | qwen config / Databricks gateway | yes | request_permission | yes | no | no | ~(text-prefix replay) | no | ? | no (markers) |
| goose | ACP subprocess | no | yes (GOOSE_MODEL) | goose config / keyring | yes | request_permission | yes | no | no | ~(text-prefix replay) | no | ? | yes |
| hermes | CLI subprocess | no (shell pre_tool_call hook) | yes (HARNESS_HERMES_MODEL) | hermes config | yes | ~(pre_tool_call hook DENY) | no | no | no | no (vendor session store) | no | ? | ? |
| antigravity | SDK in-process (localharness binary) | no (SDK in-proc tools) | yes (Gemini-only) | Gemini key / Vertex AI | yes | ~(policy DENY) | yes | no | yes | yes (seeds history into prompt) | no | yes | ?(text-flattened) |
| copilot | SDK in-process | no (SDK tool handlers) | ~(databricks-* -> auto) | GitHub token | yes | ~(pre-gated; native bypass) | yes | no | no | no (vendor CopilotSession) | no | yes | ~(flattened) |
| openai-agents | SDK in-process | no (SDK FunctionTool) | yes (multi-model) | Databricks gateway / OpenAI key | yes | canUseTool ASK | yes | no | no | yes (full history via SDK session) | yes (CompactionComplete) | ~(emitted; not surfaced) | yes |

### Transport types

| Type | Description | Examples |
|---|---|---|
| **SDK in-process** | Harness runs inside the Omnigent Python process via a vendor SDK | claude-sdk, cursor, antigravity, copilot, openai-agents |
| **CLI subprocess** | Harness spawns a vendor CLI binary and communicates via stdout/stdin (JSONL, stream-json, or shell hooks) | codex, pi, kimi, hermes |
| **ACP subprocess** | Harness uses the Agent Communication Protocol over a subprocess | qwen, goose |

### MCP connectivity

- **In-proc SDK MCP server** — the harness runs an MCP server in-process and the SDK connects to it directly (e.g. claude-sdk).
- **Non-MCP bridges** — many harnesses use vendor-specific tool bridging: `dynamicTools` RPC (codex), SDK `custom_tools` (cursor), SDK `FunctionTool` (openai-agents), TCP socket (pi), shell hooks (hermes), SDK in-proc tools (antigravity), SDK tool handlers (copilot).

### Policies / elicitation strategies

| Strategy | How it works | Harnesses |
|---|---|---|
| `canUseTool ASK` | Omnigent asks the model whether a tool call should proceed; model responds with ASK to surface to user | claude-sdk, codex, openai-agents |
| `request_permission` | ACP-native permission request flow | qwen, goose |
| 2-stage + card | Two-phase approval with a UI card | cursor |
| Pre-tool hook | Shell hook runs before each tool call; can DENY | hermes |
| Policy DENY | Omnigent policy engine denies disallowed calls | antigravity |
| Pre-gated | Tools are pre-approved; native tools bypass gating | copilot |

### Resume / fork strategies

| Strategy | How it works | Harnesses |
|---|---|---|
| Full history replay | Replays the entire message history into a fresh thread/session | codex, cursor, openai-agents |
| History prefix replay | Replays a prefix of the history into a fresh session | pi |
| Text-prefix replay | Injects a text summary/prefix of prior history | qwen, goose |
| Prompt seeding | Seeds prior history into the system prompt on rebuild | antigravity |
| Vendor session ID | Relies on the vendor's own session persistence (no Omnigent-side rebuild) | kimi, hermes, copilot |

### Auth patterns

| Pattern | Examples |
|---|---|
| Anthropic API key / Databricks gateway | claude-sdk |
| Vendor API key (direct) | cursor (Cursor API key), antigravity (Gemini key) |
| Vendor CLI login / config file | hermes, kimi, goose, qwen, pi |
| OAuth / GitHub token | copilot (GitHub PAT with Copilot permission) |
| Gateway + fallback | codex (Databricks gateway / codex auth.json), pi (gateway / API keys) |

### Checklist for a new SDK/subprocess harness

All capabilities are **required** for a complete harness integration:

- [ ] Connects to Omnigent MCP (in-proc SDK MCP server or vendor-specific bridge)
- [ ] Model override works (or document vendor lock-in)
- [ ] Auth is configured and documented (setup flow in `omni setup`)
- [ ] Streaming forwards to the Omnigent forwarder
- [ ] Policy / elicitation strategy is implemented for web UI
- [ ] Interrupt cancels the running turn
- [ ] Live queue supports concurrent turns
- [ ] Tool-boundary steering injects correctly
- [ ] Resume/fork rebuilds conversation from Omnigent transcript
- [ ] Compaction is surfaced (`CompactionComplete` events)
- [ ] Reasoning tokens are forwarded
- [ ] Images are forwarded (full binary preferred; path or text-flattened acceptable)
- [ ] Unit tests cover tool bridging, auth, model routing
- [ ] E2E skill exists for manual smoke-testing against a live server
- [ ] Mock LLM tests cover the happy path without real API calls

---

## Part 2 — Native harnesses

Native harnesses wrap a vendor's own TUI or server and mirror output into
Omnigent. They connect to the Omnigent MCP server via `stdio serve-mcp`
and relay the vendor's conversation into the Omnigent session.

### Capability matrix

| Capability | What it means |
|---|---|
| **Transport** | How the native harness communicates — tmux TUI, app server, HTTP/SSE, file-inject TUI |
| **Connects to Omnigent MCP** | Whether the native harness connects via `stdio serve-mcp` |
| **Model override** | User can select a model at launch or per-prompt |
| **Auth** | Vendor login / config / token |
| **Streaming (forwarder)** | `deltas` (token-level) vs `complete-only` (full response after completion) |
| **Policies / Elicitation** | Whether the native harness can gate tool calls — mirror+reply, permission.v2+reply, hook DENY, or none |
| **Interrupt** | User can abort a running turn |
| **Bidirectional sync (TUI->Omni)** | TUI output mirrors into the Omnigent conversation |
| **In-harness session-cmd sync** | Supports `clear`, `fork`, `resume`, `switch` commands from Omnigent |
| **Resume/fork from Omnigent transcript** | Can rebuild conversation from Omnigent transcript (native rebuild, or fresh launch) |
| **Compaction** | Vendor-internal compaction status |
| **Reasoning** | Model reasoning/thinking tokens are forwarded |
| **Images** | Image content is forwarded — path reference, full binary, or text-flattened |

### Current native harness status

| Harness | Owner | Transport | MCP | Model override | Auth | Streaming | Policies | Interrupt | Bidi sync | Session cmds | Resume/fork | Compaction | Reasoning | Images |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-native | Daniel Lok | tmux TUI | yes (stdio serve-mcp) | yes (--model) | Claude CLI login | deltas | yes | yes | yes | clear/fork/resume | yes (native rebuild) | vendor-internal (status only) | ? | yes (path) |
| codex-native | Zeyi Fan / Pat Sukprasert | app server | yes (stdio serve-mcp) | no (user-launched) | codex auth.json | deltas | yes (mirror+reply) | yes (turn/interrupt) | yes | yes (new/switch/fork) | yes (native fork-history rebuild) | yes | yes (in-term) | yes |
| cursor-native | Serena Ruan / Corey Zumar | tmux TUI | yes (stdio serve-mcp) | no | cursor sub/key | complete-only | no (read-only mirror) | no | yes | no | no (fork launches fresh) | no | ? | yes (path) |
| hermes-native | Tomu Hirata | tmux TUI | no | ?(set at launch) | hermes config | complete-only | no | no | yes | no | no (conversation in TUI) | no | ? | yes (path) |
| opencode-native | Dhruv Gupta | native HTTP/SSE server | no | yes (per-prompt) | OpenCode native | complete-only | yes (permission.v2+reply) | yes (abort) | yes | no | no (relies on OpenCode server session) | no | ? | yes |
| pi-native | Sabhya Chhabria | tmux TUI | no | no | pi config / managed dir | complete-only | ~(PreToolUse hook DENY) | ~(TUI only) | yes | no | no (fork launches fresh) | no | ? | yes (path) |
| **0.3.0 Release CUT LINE** | | | | | | | | | | | | | | |
| kimi-native | — | tmux TUI | no | no | kimi config | complete-only | ~(PreToolUse hook DENY) | ~(TUI only) | yes | no | no (conversation in TUI) | no | ? | yes (path) |
| qwen-native | — | file-inject TUI | no | no (--model at launch) | qwen config | complete-only | no (TUI gates) | yes (tmux) | yes | no | no (conversation in TUI) | no | ? | ? |
| goose-native | — | tmux TUI | no | no (--model at launch) | goose config | complete-only | no (GOOSE_MODE in-term) | yes (tmux) | yes | no | no (conversation in TUI) | no | ? | ? |
| antigravity-native | — | tmux TUI (RPC) | no | — (agy self-selects) | inherited from agy | deltas | no | yes (CancelCascade RPC) | no | clear only | no (agy owns cascade history) | no | no (opaque) | ~(path marker) |
| kiro-native | — | tmux TUI | no | ?(set at launch) | kiro config | complete-only | no | no | yes | no | no (conversation in TUI) | no | ? | yes (path) |

### Checklist for a new native harness

All capabilities are **required** for a complete native harness integration:

- [ ] Transport chosen and implemented (tmux TUI, app server, HTTP/SSE)
- [ ] Connects to Omnigent MCP via `stdio serve-mcp`
- [ ] Model override works (or document vendor lock-in)
- [ ] Auth configured (vendor login / config)
- [ ] Streaming forwarder works (deltas preferred; complete-only acceptable)
- [ ] Policy / elicitation strategy gates tool calls in web UI
- [ ] Interrupt aborts the running turn
- [ ] Bidirectional sync mirrors TUI output into Omnigent conversation
- [ ] Session commands (clear, fork, resume) work from Omnigent
- [ ] Resume/fork rebuilds from Omnigent transcript
- [ ] Compaction status is surfaced
- [ ] Reasoning tokens are forwarded
- [ ] Images are forwarded (path preferred; binary or text-flattened acceptable)
- [ ] Unit tests cover forwarder, auth, transport
- [ ] E2E skill exists for manual smoke-testing against a live server
- [ ] Mock LLM tests cover the happy path without real API calls
