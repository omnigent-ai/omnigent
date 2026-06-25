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

### Transport types

| Type | Description |
|---|---|
| **SDK in-process** | Harness runs inside the Omnigent Python process via a vendor SDK |
| **CLI subprocess** | Harness spawns a vendor CLI binary and communicates via stdout/stdin (JSONL, stream-json, or shell hooks) |
| **ACP subprocess** | Harness uses the Agent Communication Protocol over a subprocess |

### MCP connectivity

- **In-proc SDK MCP server** — the harness runs an MCP server in-process and the SDK connects to it directly (e.g. claude-sdk).
- **Non-MCP bridges** — many harnesses use vendor-specific tool bridging: `dynamicTools` RPC (codex), SDK `custom_tools` (cursor), SDK `FunctionTool` (openai-agents), TCP socket (pi), shell hooks (hermes), SDK in-proc tools (antigravity), SDK tool handlers (copilot).

### Policies / elicitation strategies

| Strategy | How it works |
|---|---|
| `canUseTool ASK` | Omnigent asks the model whether a tool call should proceed; model responds with ASK to surface to user |
| `request_permission` | ACP-native permission request flow |
| 2-stage + card | Two-phase approval with a UI card |
| Pre-tool hook | Shell hook runs before each tool call; can DENY |
| Policy DENY | Omnigent policy engine denies disallowed calls |
| Pre-gated | Tools are pre-approved; native tools bypass gating |

### Resume / fork strategies

| Strategy | How it works |
|---|---|
| Full history replay | Replays the entire message history into a fresh thread/session |
| History prefix replay | Replays a prefix of the history into a fresh session |
| Text-prefix replay | Injects a text summary/prefix of prior history |
| Prompt seeding | Seeds prior history into the system prompt on rebuild |
| Vendor session ID | Relies on the vendor's own session persistence (no Omnigent-side rebuild) |

### Auth patterns

| Pattern | Description |
|---|---|
| API key / Databricks gateway | Direct API key or routed through a Databricks gateway |
| Vendor API key (direct) | Vendor-specific API key (e.g. Cursor, Gemini) |
| Vendor CLI login / config file | Credentials stored in a vendor config file or managed via vendor CLI login |
| OAuth / GitHub token | OAuth flow or platform token (e.g. GitHub PAT) |
| Gateway + fallback | Primary gateway with fallback to vendor-native auth |

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
