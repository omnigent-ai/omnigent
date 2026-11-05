# Omnigent tools under Claude Code's managed-mcp.json

An organization can pin Claude Code's MCP servers with an enterprise
`managed-mcp.json` (`/Library/Application Support/ClaudeCode/managed-mcp.json` on
macOS, `/etc/claude-code/managed-mcp.json` on Linux). While that file exists, Claude
Code loads only the servers it lists. It ignores user, project and plugin servers, and
it refuses `--mcp-config`, which is how Omnigent normally hands each claude-sdk and
claude-native session its own tools (`sys_session_*`, `sys_os_*`, skills and the rest).

To keep those tools, add one static `omnigent` entry to the file. Omnigent sets the
variables it reads for each session, so the same entry serves every session and
every user on the machine.

## The entry

Merge this into the file's `mcpServers` and keep the organization's other servers:

```json
{
  "mcpServers": {
    "omnigent": {
      "command": "${OMNIGENT_MCP_RELAY_PYTHON}",
      "args": [
        "-I",
        "-m",
        "omnigent.harnesses.claude_native.bridge",
        "serve-mcp",
        "--bridge-dir",
        "${OMNIGENT_MCP_RELAY_BRIDGE_DIR}"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": "${OMNIGENT_MCP_RELAY_TMPDIR}"
      }
    }
  }
}
```

- Name the entry `omnigent`. Sessions call these tools `mcp__omnigent__<tool>`, and
  Omnigent looks for this name to decide whether the entry is deployed.
- Keep the `${...}` references as written. Claude Code expands them from its own
  environment when it starts:

| Variable | Set by Omnigent to |
| --- | --- |
| `OMNIGENT_MCP_RELAY_PYTHON` | The Python interpreter that runs Omnigent. |
| `OMNIGENT_MCP_RELAY_BRIDGE_DIR` | The session's private relay directory, which also holds the relay's access token. |
| `OMNIGENT_MCP_RELAY_TMPDIR` | The temporary directory Omnigent's relay directories live under. |

- If managed settings define `allowedMcpServers`, also list
  `{"serverName": "omnigent"}` there. Claude Code applies that allowlist to any
  managed entry that uses `${VAR}`, and drops the entry silently when it isn't listed.
- Don't name `omnigent` in `deniedMcpServers`. The denylist wins over the allowlist,
  and Claude Code drops a denied entry just as silently. Omnigent reads only
  `managed-mcp.json`, not this policy, so with the entry denied or left off the
  allowlist, sessions start without Omnigent's tools and without the missing-entry
  warning, and claude-sdk still tells the model the tools exist.

> **Security note.** This entry hands its executable (`${OMNIGENT_MCP_RELAY_PYTHON}`)
> and its tool surface (the bridge directory) to whatever environment launches Claude
> Code. A Claude started separately can set these variables and run another program
> under the approved `omnigent` name. An organization that needs an
> administrator-fixed tool catalog shouldn't deploy this entry.

## What happens

- **With the entry:** claude-sdk sessions start with Omnigent's tools, and
  claude-native sessions get them back.
- **Without it:** sessions still start, but without Omnigent's tools. The runner
  logs a warning that points to this page.
- **Outside Omnigent:** a plain `claude` started by hand doesn't have these
  variables, so `claude mcp list` reports the `omnigent` entry as failed to connect.
  Nothing else is affected.

`/mcp` in an Omnigent session lists only the servers in `managed-mcp.json` while the
file exists, because those are the only ones Claude Code loads. The `omnigent` entry
itself is never listed. To report sign-in state for the managed servers, see
[MCP_AUTH_STATUS.md](MCP_AUTH_STATUS.md).

## Tool call time limit

Through this entry, each Omnigent tool call runs in the relay with a budget of 300
seconds of active work. Time spent waiting for an approval doesn't count. When a
call runs past it, Omnigent asks the tool to stop and returns an error: "Omnigent
tool dispatch timed out after 300s of active work (approval waiting excluded).
Cancellation requested; completion is unknown." The work may have finished in
part, so check its effects before retrying. This applies even to a tool given a
longer timeout of its own, such as a `sys_os_shell` command. No setting changes
the budget. Claude Code's own MCP tool timeout (`MCP_TOOL_TIMEOUT`, or a
per-server timeout) also applies, so whichever is shorter ends the call.

Without a managed file, claude-sdk runs Omnigent's tools in-process, where this
relay budget doesn't apply.
