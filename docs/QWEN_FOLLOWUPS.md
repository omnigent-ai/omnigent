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

The RPC mode executor should expose multimodal capabilities when available:

- [ ] Add image input support for Qwen's vision models
- [ ] Expose `handles_tools_internally()` capability flag properly
- [ ] Test multimodal tool calling with Qwen models

### Native TUI Integration

Currently uses the same terminal attachment as other SDK harnesses:

- [ ] Implement proper tmux-based terminal attach (like pi-native)
- [ ] Add native TUI glyph/icon for agent picker
- [ ] Support native terminal session management

### Advanced RPC Features

The RPC mode design enables mid-turn interrupt and tool exposure:

- [ ] Implement mid-turn interrupt handling over the RPC bridge
- [ ] Expose tools directly through MCP instead of internal routing
- [ ] Add reconnect logic for dropped RPC connections
- [ ] Support port binding configuration (currently auto-assigned)

### Test Coverage

Expand test suite to match Kimi's 38-test coverage:

- [ ] Registry/allowlist tests (import guards)
- [ ] FastAPI app shape tests (/health, /v1/sessions/{id}/events routes)
- [ ] Env-var factory tests (HARNESS_QWEN_* → executor kwargs)
- [ ] _build_argv tests (every flag passed to qwen)
- [ ] Event translator tests (all event types parsed)
- [ ] run_turn end-to-end with stubbed subprocess
- [ ] Missing-binary error path tests
- [ ] Capability flags tests (handles_tools_internally, supports_streaming)

## Known Limitations

### RPC Mode Complexity

Qwen's choice of `qwen --mode rpc` (long-lived subprocess + tool bridge over TCP)
is structurally more ambitious than per-turn subprocess models. This introduces:

- More places to get wrong: auth, lifecycle, reconnect, port binding, token handling
- Requires more comprehensive error handling and recovery
- Testing complexity is higher than simpler subprocess models

### Databricks Gateway Path

The current implementation supports Databricks via profile or model prefix,
but may need refinement based on real-world usage:

- [ ] Verify `databricks-*` model prefix routing works correctly
- [ ] Test with multiple Databricks profiles
- [ ] Validate auth command integration

## Implementation Notes

### RPC Bridge Design

The RPC mode uses a TCP socket bridge for tool calls. Key considerations:

1. **Port binding**: Currently auto-assigned; consider configurable range
2. **Auth token**: Should be passed securely to the Qwen subprocess
3. **Lifecycle management**: Ensure clean shutdown of both subprocess and bridge
4. **Reconnect logic**: Handle dropped connections gracefully

### Model Override Behavior

Qwen's per-session model override (`--model`) should work identically to other
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
