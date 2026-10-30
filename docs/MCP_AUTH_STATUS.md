# Reporting MCP account state to a host

`/mcp` shows whether each MCP server a session can use is connected. For servers the
host owns (Claude Code, Codex and Cursor configs, including an organization's
`managed-mcp.json`), Omnigent has no way to know that state: whatever manages those
sign-ins does. This file is how that manager tells Omnigent.

## Where it lives

`~/.omnigent/mcp-auth-status.json`, or the absolute path in
`OMNIGENT_MCP_AUTH_STATUS_FILE` when `$HOME` is read-only or sandboxed. The path is
used as given, so a relative value resolves against the host process's working
directory.

## Format

```json
{
  "servers": [
    { "harness": "claude", "name": "linear", "status": "sign_in_required",
      "sign_in_url": "https://auth.example.com/start?state=abc" },
    { "harness": "codex", "name": "glean", "status": "connected" }
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `harness` | yes | `claude`, `codex` or `cursor`: which harness loads the server. |
| `name` | yes | The server's name as the harness config spells it. |
| `status` | yes | `connected` or `sign_in_required`. |
| `sign_in_url` | no | Page that completes the sign-in. Only read with `sign_in_required`. |

The panel renders `sign_in_required` as an amber row with a **Sign in** link that opens
`sign_in_url` in a new tab. While the panel is open and a host server for the session's
harness is reported as `sign_in_required`, it polls this file (through the host inventory) every few seconds, so flipping the entry
to `connected` updates the row.

## Rules the reader enforces

- An entry naming a server the host did not discover is ignored. Report servers under
  the name and harness the config uses.
- A `status` outside the two values above is ignored, so a newer manager can add one
  without breaking an older Omnigent: the row simply shows no state.
- `sign_in_url` must be an absolute `http(s)` URL with a hostname, no embedded
  credentials, and no whitespace or control characters. Anything else is dropped and
  the row shows the state with nothing to click. The same rule is applied again where
  the host frame is decoded, so a client never depends on the writer behaving.
- The file must be a regular file of at most 64 KiB. A FIFO, a device, an oversized
  file or unparsable JSON is ignored, and the servers are reported without state.

## Writing it

Replace the file atomically: write a temporary file in the same directory and rename it
over this path. A partial write is unparsable, and the panel would show no state until
the next good read.

Entries are matched on `(harness, name)` only. Two servers sharing a name under one
harness, such as a user server and a plugin server, cannot be told apart and both take
that entry's state. If the file lists the same `(harness, name)` more than once, the
last valid entry wins.

Anything that can write this file is trusted as far as the harness configs beside it:
it can mark a known server as needing sign-in and name an http(s) page for it. It
cannot add servers, change where one points, or reach any other part of a session.
