# Workspace fast track

The file viewer can read a session's workspace files through the workspace API
(`omnigent/api/workspace/v1alpha1/workspace.proto`) instead of the REST file
routes. The session's host answers every call with one implementation
(`omnigent/host/workspace_service.py`), whichever route carried it.

## Routes

Each read uses the first route that works:

| Route | When | Path of the file's bytes |
|---|---|---|
| Direct, local socket | Desktop app, host on the same machine | host → desktop |
| Direct, over SSH | Desktop app, host on a machine this desktop connected with "Connect over SSH" | host → SSH → desktop |
| Server workspace API | Any client, or the desktop when direct fails | host → host tunnel → server → client |
| REST file routes | Everything else, and paths outside the workspace | runner (or host) → server → client |

Direct reads keep file bytes off the server: it only issues a token. The server
API skips the runner and streams protobuf messages through without re-encoding
them. A route that fails for a session is skipped for five minutes. Error
answers such as "not found" go to REST, so callers keep the errors they expect.

## Turning it on

| Where | What |
|---|---|
| Server | `OMNIGENT_FEATURES=workspace_direct_access,workspace_api_server_access` |
| Server | `omnigent[workspace-api]`, for the server API route only |
| Hosts | `omnigent[workspace-api]`. A host with it always opens its workspace socket; it has no flag of its own. |
| Desktop app | A release with the workspace socket bridge. Older shells fall back to the server routes. |

Both flags are off by default and appear in `/v1/info`, which is how the page
picks its route. Turn one off to stop that route for every client at once.

`omnigent[workspace-api]` adds `connectrpc` and its HTTP client, `pyqwest`.
On Linux older than glibc 2.28, `pyqwest` has no prebuilt wheel and builds from
source with Rust.

## How direct reads work

1. The page asks the server for access: `POST /v1/sessions/{id}/workspace/direct-access`.
   The server answers only the session's owner, only for unconfined sessions,
   and only when the host advertises a socket. It returns the host id, the
   socket path on the host's machine and a token valid for five minutes.
2. The page hands the call to the desktop shell
   (`window.omnigentDesktop.workspaceSocketRequest`), because pages can't open
   Unix sockets.
3. Electron main (`web/electron/src/workspaceSocket.js`) sends it:
   - to a socket on this machine, if it is `omnigent-workspace-*/workspace.sock`
     in a directory only this user can enter;
   - otherwise over SSH, if this desktop connected the host with "Connect over
     SSH" (`web/electron/src/workspaceSshForwards.js`). Main keeps one shared,
     prompt-free connection per machine and forwards the host's socket to a
     private local one.
4. The host checks the token, then reads the file from the token's workspace.

## Security

- **Tokens.** HS256, signed with a key the host sends the server when it
  connects. They name one host, session and workspace, and last five minutes. A
  host restart invalidates them.
- **Sockets.** The host's socket lives in a 0700 directory. Main only forwards
  workspace API methods and their own headers, with size limits.
- **SSH destinations.** Main records which destination reaches each host it
  connected (`ssh-hosts.json` in the app's data folder, mode 0600), per server.
  Pages and servers only ever name a host id, never a destination.
- **No new trust prompts.** SSH runs with `BatchMode=yes` and the user's own
  SSH config. Anything that would prompt fails, and the read goes through the
  server.
- **Confined sessions** always use the REST routes, which apply their sandbox.

## Checking it

- `curl -s <server>/v1/info` lists both flags under `features`.
- On the host's machine, `ls -d "${TMPDIR:-/tmp}"/omnigent-workspace-*` shows
  the socket directory.
- In the desktop app's DevTools, open a file. A direct read shows one
  `POST …/workspace/direct-access` and no file request; calls to the desktop
  shell don't appear in the Network tab. A server API read shows
  `…/WorkspaceService/ReadFile`.

| Symptom | Likely cause |
|---|---|
| `…/filesystem/…` requests only | Flags off, an old page, a confined session, or a path outside the workspace |
| `ReadFile` instead of a direct read | Direct failed. Electron main logs why in its console. |
| `not a workspace socket on this machine` in main's log | The host is on another machine and this desktop has no SSH record for it |
| `ReadFile` answers `unimplemented` | The host lacks `omnigent[workspace-api]` or predates the workspace API |

## Not covered yet

- Files tree listings still use REST.
- Hosts connected some other way than "Connect over SSH" use the server API route.
- Windows has no direct route.
