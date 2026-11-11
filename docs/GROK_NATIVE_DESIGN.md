# grok-native design

Feature request: #13541. Precedent PRs: commandcode-native (#13163),
opencode-native (`native-server` transport), devin-native (superseded its ACP
row).

## Verdict from the live spike

`grok agent serve --bind 127.0.0.1:<port> --secret <token>` boots a WebSocket
server at `ws://<addr>/ws?server-key=<token>` and speaks **ACP over WS**: an
`initialize` JSON-RPC call returns `protocolVersion: 1` with

- `agentCapabilities.loadSession`, `sessionCapabilities.list/resume/close`
- `mcpCapabilities` http + sse (→ can wire the Omnigent MCP server in-band)
- `promptCapabilities.image`, `embeddedContext`
- `authMethods` (`cached_token` from `~/.grok/auth.json`, `grok.com`)
- `_meta["x.ai/hooks"]`: client-registered hooks —
  `blockingEvents: [pre_tool_use, stop, subagent_stop]`,
  `decisions: [deny, block]`,
  `stopSignals: [continue, stopReason, additionalContext]`

So the harness is a `native-server` transport (second after opencode-native),
NOT a tmux TUI wrap. The ACP executor machinery may be largely reusable with a
WS transport instead of stdio; the differentiators over the builtin ACP row
are the `x.ai/hooks` extension (policy + elicitation), MCP wiring, and the
vendor session store.

## Capability targets

| Axis | Plan |
|---|---|
| Transport | `native-server` (`grok agent serve`, WS, per-session secret) |
| Policies | `x.ai/hooks` client-registered `pre_tool_use` → ALLOW/DENY/ASK (`permissionDecision`), `stop` for turn-boundary gates |
| Elicitation | ASK via `pre_tool_use` `deny`/`block` + Omnigent permission card; verify the ASK verdict exists over WS (hook doc lists `ask` client-side) |
| Omnigent MCP | `mcpCapabilities` http/sse → `sys_*` tools incl. `sys_read_inbox` (first native harness to expose the mailbox natively) |
| Inbox wake | `stop` hook: block turn end with `stopReason` fed to model while mail pending |
| Instructions | `SessionStart`/additionalContext (filesystem hooks as fallback) |
| Streaming | ACP `session/update` notifications — verify token deltas over WS |
| Resume/fork | `session/load` + `sessionCapabilities.resume`; `--fork-session` for clones |
| Cost | `grok usage` / usage events if emitted over WS |
| Interrupt | ACP `session/cancel` |
| Fork history | TBD (`_FH`) |

## Open questions

1. Does `session/new`/`load` over WS accept client-registered hooks directly
   (`_meta.x.ai/hooks` registration call), or only via config layers?
2. Do tool-call `ask` verdicts round-trip as an Omnigent elicitation
   (`session/request_permission` exists in ACP — prefer that if present)?
3. Does the WS surface expose usage/cost events, or do we tail `grok usage`?
4. Folder-trust: do client-registered WS hooks bypass the `.grok/hooks` trust
   gate (README says "ACP client-registered hooks are unaffected")?

## Implementation sketch

- `omnigent/harnesses/grok_native/` — serve spawn (port alloc, secret mint),
  WS client + forwarder (ACP `session/update` → `external_conversation_item`),
  hook bridge (registered hooks → `/v1/sessions/{id}/policies/evaluate` +
  `hooks/permission-request`), lifecycle (`session/close` on teardown)
- `omnigent/inner/grok_native_executor.py` + `grok_native_harness.py`
- Registry: `NativeCodingAgent(key="grok")` — the `grok` ACP row already owns
  the `grok` alias for `omnigent run --harness`; native launcher is
  `omnigent grok` (dispatch prefers native, like goose/devin)
- Capability: `_IM.NATIVE_SERVER`, `_EL.<hook-or-permission>`,
  `_RS.WARM_REATTACH`, `fork_history=_FH.TBD`, `streaming=True` (verify)
- Runner: opencode-style serve-process resource (not a tmux pane)
- Tests: WS protocol fakes (fixture NDJSON like opencode's `events.ndjson`),
  policy hook round-trip, forwarder item mapping
- feature-map row; README roster line only if/when the launcher lands
