# OpenClaw integration

Omnigent can work with OpenClaw in two different ways:

| Goal | What Omnigent drives | Setup |
|---|---|---|
| Use coding agents registered in OpenClaw/acpx | Each agent's ACP command directly | Import the registry or use `--from-openclaw` |
| Use OpenClaw's routing, memory, and channels | The live OpenClaw Gateway through `openclaw acp` | Register the Gateway bridge as an `acp:` agent |

Choose the first option when you want a coding agent such as Codex or Gemini
inside Omnigent. Choose the second when OpenClaw itself is the assistant you
want to reach from Omnigent.

## Import coding agents

Omnigent can read agent commands from either of OpenClaw's supported ACP
registry locations:

- `~/.acpx/config.json`
- `~/.openclaw/openclaw.json`

To import the discovered agents into `~/.omnigent/config.yaml`, run:

```bash
omni setup
```

Open **Configure harnesses**, select **Import from OpenClaw**, choose the
detected registry, and confirm the agents to import. Each imported agent appears
as `acp:<slug>` in Omnigent's harness picker and keeps its own authentication.
Omnigent stores the launch command, not the agent's credentials.

For a one-off run without changing Omnigent's config, address an agent by its
OpenClaw/acpx registry name:

```bash
omni run --from-openclaw "Gemini CLI"
```

This path drives the selected coding agent directly. It does not bring
OpenClaw's Gateway session, memory, routing, or channels into the conversation.

## Drive the OpenClaw Gateway

The `openclaw acp` command exposes a live OpenClaw Gateway session as an ACP
server over stdio. Register that command in `~/.omnigent/config.yaml`:

```yaml
acp:
  agents:
    - name: OpenClaw
      command: openclaw acp --url <gateway-url> --token-file <token-file>
      omnigent_mcp: false
```

> [!CAUTION]
> Prefer `--token-file` so the Gateway token is not stored in the launch
> command. Do not commit or share `~/.omnigent/config.yaml`, and use a token
> with the narrowest permissions OpenClaw supports.

Replace `<gateway-url>` and `<token-file>` with the connection details for your
running Gateway, then launch it with:

```bash
omni run --harness acp:openclaw
```

The connection is a hub over a hub:

```text
Omnigent --ACP over stdio--> openclaw acp --WebSocket--> OpenClaw Gateway
                                                        |-- routing
                                                        |-- memory
                                                        `-- channels and agents
```

### Why `omnigent_mcp` must be false

Omnigent normally lends its builtin tools to ACP agents by including
`mcpServers` in `session/new`. OpenClaw's Gateway bridge rejects per-session MCP
servers, so the OpenClaw entry must set `omnigent_mcp: false`. OpenClaw keeps
using its own tools; the setting only disables Omnigent's additional MCP relay
for this agent.

### Session and tool boundaries

Omni owns the outer conversation and transcript; OpenClaw owns the
Gateway-backed ACP session. OpenClaw's Control UI is a separate Gateway client,
not a second view that Omni continuously mirrors. Messages entered in the
Control UI while Omni is offline are therefore not imported into the Omni
conversation. Use Omni as the canonical conversation when you need its
transcript and resume behavior.

With `omnigent_mcp: false`, OpenClaw still has its own native tools (for
example, shell and filesystem tools), but it does not receive Omnigent's
additional MCP relay. Enabling the setting is currently incompatible with
OpenClaw's Gateway ACP bridge because that bridge rejects per-session MCP
servers.

### Compatibility status

The protocol-level path matches for initialization, session creation, prompts,
cancellation, and streaming updates. Manual validation against a live Gateway
has also confirmed the configured worktree, native shell/filesystem tools, and
Omni conversation resume. Bidirectional synchronization with the OpenClaw
Control UI and per-session Omnigent MCP are not supported by this integration.
OpenClaw cannot be installed or run in the project's managed development and CI
environments.

If session creation reports that `mcpServers` is unsupported, confirm that the
entry contains `omnigent_mcp: false` and restart the Omnigent session. If the
turn reaches OpenClaw but no final reply appears, capture both Omnigent and
OpenClaw logs and report the behavior before relying on the integration.
