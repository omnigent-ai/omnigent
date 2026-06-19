# Qwen Integration Follow-ups

This document tracks deferred work for the Qwen Code integration.

## Deferred Features

### Provider Injection (Priority: High)

The current implementation routes Qwen through OpenAI-compatible providers
(key/gateway/local). Full provider injection like Kimi v1 should be added:

- [ ] Add `configure_agent_harness_with_provider` branch for "qwen" harness
  that handles provider routing (similar to `_apply_provider_to_pi`)
- [ ] Support gateway base URL configuration via provider config
- [ ] Support dynamic auth commands via provider config

### Multimodal Support

The ACP executor should expose multimodal capabilities when available:

- [ ] Add image input support for Qwen's vision models
- [ ] Expose `handles_tools_internally()` capability flag properly
- [ ] Test multimodal tool calling with Qwen models

### Native TUI Integration

Currently uses the same terminal attachment as other SDK harnesses:

- [ ] Implement proper tmux-based terminal attach (like pi-native)
- [ ] Add native TUI glyph/icon for agent picker
- [ ] Support native terminal session management

### Advanced ACP Features

The ACP (Agent Communication Protocol) design enables mid-turn interrupt and
tool exposure via JSON-RPC 2.0 over stdin/stdout (`qwen --acp`):

- [ ] Implement mid-turn interrupt handling (ACP `session/cancel` notification)
- [ ] Expose Omnigent tools to Qwen via MCP (register as ACP MCP servers)
- [ ] Add reconnect logic if the subprocess exits unexpectedly
- [ ] Support `session/load` for multi-turn resume across runs

### Test Coverage

Expand test suite to match Kimi's 38-test coverage:

- [ ] Registry/allowlist tests (import guards) ✅ done
- [ ] FastAPI app shape tests (/health, /v1/sessions/{id}/events routes) ✅ done
- [ ] Env-var factory tests (HARNESS_QWEN_* → executor kwargs)
- [ ] _build_qwen_argv tests (ACP flags passed to qwen)
- [ ] Event translator tests (all ACP session update types parsed) ✅ done
- [ ] run_turn end-to-end with stubbed ACP subprocess ✅ done
- [ ] Missing-binary error path tests
- [ ] ACP session-not-found auto-reset test ✅ done

## Known Limitations

### ACP Protocol Constraints

Qwen uses `qwen --acp` (ACP / JSON-RPC 2.0 over stdin/stdout). Key notes:

- Tool calls Qwen makes internally are handled by Qwen itself — the current
  executor observes `tool_call` notifications but does not intercept them.
  Future work: register Omnigent tools as ACP MCP servers so they route
  through Omnigent's policy/recording stack.
- Qwen assigns its own `sessionId` in the `session/new` response; the id we
  propose is treated as a hint only.
- Permissions requests (`session/request_permission`) are currently
  auto-approved. Proper integration with Omnigent's policy layer is deferred.

### Databricks Gateway Path

The current implementation supports Databricks via profile or model prefix,
but may need refinement based on real-world usage:

- [ ] Verify `databricks-*` model prefix routing works correctly
- [ ] Test with multiple Databricks profiles
- [ ] Validate auth command integration

## Implementation Notes

### ACP Protocol Design

The executor uses `qwen --acp` (Agent Communication Protocol, JSON-RPC 2.0
over newline-delimited JSON on stdin/stdout). The session lifecycle is:

1. `initialize` — capability handshake (one-time per subprocess)
2. `session/new { cwd, mcpServers }` — create a session; server returns its
   own `sessionId`
3. `session/prompt { sessionId, prompt: [{type,text}] }` — send a user turn;
   streaming `session/update` notifications flow back, final response resolves
   the request
4. The subprocess is kept alive across turns (no per-turn respawn)

### Model Override Behavior

Qwen's per-session model override should work identically to other
SDK harnesses:

- Spec model → provider default → catalog default (precedence)
- `/model` REPL command overrides all above via env var
- Native CLI uses `--model` argv, SDK uses HARNESS_QWEN_MODEL env var

### Environment Variables

The following env vars are supported (mirroring Kimi's pattern):

- `HARNESS_QWEN_MODEL`: Model ID override
- `HARNESS_QWEN_GATEWAY_BASE_URL`: Gateway endpoint
- `HARNESS_QWEN_DATABRICKS_PROFILE`: Databricks profile name
- `HARNESS_QWEN_SKILLS_FILTER`: Skills whitelist/blacklist
- `HARNESS_QWEN_AGENT_NAME`: Agent display name
- `HARNESS_QWEN_BUNDLE_DIR`: Bundle directory for bundled skills
