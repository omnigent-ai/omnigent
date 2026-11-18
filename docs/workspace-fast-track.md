# Workspace fast track

The file viewer and the Files tree can read a session's workspace through the
workspace API (`omnigent/api/workspace/v1alpha1/workspace.proto`) instead of the
REST file routes: file reads (`ReadFile`) and directory listings
(`ListDirectory`, including the checks for files mentioned in chat). The
session's host answers every call with one implementation
(`omnigent/host/workspace_service.py`), whichever route carried it. Its listings
match the runner's REST listings; `tests/runner/test_environment_filesystem.py`
compares them.

## Routes

Each read uses the first route that works:

| Route | When | Path of the file's bytes |
|---|---|---|
| Direct, local socket | Desktop app, host on the same machine | host → desktop |
| Direct, over SSH | Desktop app, host on another machine it reaches over SSH (see below) | host → SSH → desktop |
| Server workspace API | Any client, or the desktop when direct fails | host → host tunnel → server → client |
| REST file routes | Everything else, and paths outside the workspace | runner (or host) → server → client |

Direct reads keep file bytes off the server: it only issues a token. The server
API skips the runner and streams protobuf messages through without re-encoding
them. A route that fails for a session is skipped for five minutes. Error
answers such as "not found" go to REST, so callers keep the errors they expect.

## Methods a host serves

`ListDirectory` and `ReadFile` came with the API, so every host that serves it
has them. Before any other method (file and content search, Git comparisons)
the page asks the route's host with `GetCapabilities`, once per route every
five minutes, shared by concurrent calls and never cancelled by one of them.
A method or feature the host doesn't list goes straight to REST, or reads as
unavailable where REST has no equivalent. An `unimplemented` answer, or one the
page can't use (a content search that ends early), skips only that method on
that route for five minutes. Either way the route keeps serving what the host
has, so an older host still reads files. Git comparisons have no REST
equivalent, so a request the host refuses, such as an unknown revision, fails
with the host's message instead.

Go to File and Content search try the workspace API first and fall back to
their REST routes. Content search is only asked for while the server's
`workspace_content_search` flag is on. Neither route can stop a search on the
host once it has started: a search the user abandons runs until it ends on
its own.

## Turning it on

| Where       | What                                                                                                    |
| ----------- | ------------------------------------------------------------------------------------------------------- |
| Server      | `OMNIGENT_FEATURES=workspace_direct_access,workspace_api_server_access`                                 |
| Server      | `omnigent[workspace-api]`, for the server API route only. `omnigent[databricks]` includes it.           |
| Hosts       | `omnigent[workspace-api]`, or `omnigent[databricks]`. A host with it always opens its workspace socket. |
| Desktop app | A release with the workspace socket bridge. Older shells fall back to the server routes.                |

Both flags are off by default and appear in `/v1/info`, which is how the page
picks its route. Turn off `workspace_direct_access` to stop new direct-access
grants for every client at once; a token already issued keeps working until it
expires, up to five minutes later. Turn off `workspace_api_server_access` to
stop the server workspace API route. Only `workspace_direct_access` governs
direct-access tokens.

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
   - otherwise over SSH (`web/electron/src/workspaceSshForwards.js`), to the
     destination recorded for the host. Main keeps one shared, prompt-free
     connection per machine and forwards the host's socket to a private local one.

   A host gets a recorded destination in one of two ways:
   - **"Connect over SSH"** records the destination it started the host on.
   - **The host's SSH alias.** For a host with no record, the file viewer passes
     the alias the web app infers for it, the same one the "Open in" editor
     links use (`web/src/hooks/useHostSshTarget.ts`). Main first asks the user
     in a native dialog, and records the alias only after a token-checked
     `ListDirectory` call over it succeeds (`web/electron/src/sshTargets.js`).
     A server has one question open at a time. Its other suggestions read
     through the server meanwhile, without the page backing off, and are asked
     on a later read. After "Not now", that server's aliases aren't asked about
     again until the app restarts. An allowed alias that fails isn't tried
     again for five minutes. If the host refuses the probe's token, main
     answers the call with a 401 itself, so the page renews its token, and the
     call never reaches the alias. A second refusal within five minutes counts
     as a failure.
4. The host checks the token, then reads the file from the token's workspace.

## Security

- **Tokens.** HS256, signed with a key the host sends the server when it
  connects. They name one host, session and workspace, and last five minutes. A
  host restart invalidates them.
- **Sockets.** The host's socket lives in a 0700 directory. Main only forwards
  workspace API methods and their own headers, with size limits.
- **SSH destinations.** Main records which destination reaches each host
  (`ssh-hosts.json` in the app's data folder, mode 0600), per server. A page
  can suggest any destination without a user or options, such as an alias or a
  host name. Main runs no SSH for it, so no config, proxy or `Match exec`
  either, until the user allows it in a native dialog. The user's consent is
  the only check on which destination is tried. Main records it only after the
  host accepts that session's token over it. After "Not now", the server gets
  no more questions until the app restarts, so it can't keep asking with new
  names.
- **No password or host-key prompts.** SSH runs with `BatchMode=yes` and the
  user's own SSH config, so a password or an unknown host key fails, and the
  read goes through the server. An SSH agent's confirmation dialog, or a
  `ProxyCommand` that talks to the user, can still appear.
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
| `not a workspace socket on this machine` in main's log | The host is on another machine, and it has no record and no SSH alias |
| `SSH alias not allowed or not verified` in main's log | The user chose "Not now", or the alias didn't reach the host |
| `ReadFile` answers `unimplemented` | The host lacks `omnigent[workspace-api]` or predates the workspace API |

## Not covered yet

- Hosts on other machines with no record and no inferable alias use the server API route.
- Windows has no direct route.
