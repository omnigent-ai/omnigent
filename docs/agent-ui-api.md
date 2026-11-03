# Agent file-opening API

Agents can request that Omnigent open a file in the workspace viewer. This uses
an existing session; it does not launch an app, switch to another session, read
file contents back to the agent, or modify the file.

## Agent tool

`ui_open_file` is automatically available to agents, including through the native
harness relay. No agent YAML configuration is needed.

```json
{"path": "docs/guide.md"}
```

Paths are relative to the **calling session's workspace**, not the agent's shell
current directory. Use forward slashes. Absolute paths, `~`, `..` traversal,
URLs, backslashes, colons, control characters and empty paths are rejected. The
path is limited to 4096 characters. The tool cannot target another session.

## HTTP endpoint

```sh
curl -X POST "$OMNIGENT_SERVER/v1/sessions/$SESSION_ID/ui/open-file" \
  -H "Authorization: Bearer $OMNIGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"path":"docs/guide.md"}'
```

Use the deployment's normal authentication (omit the authorization header for a
local single-user server). The caller must have **edit access** to the session.

The endpoint returns HTTP **202 Accepted**:

```json
{"subscribers": 1}
```

`subscribers` counts live session-stream subscriptions to which delivery was
scheduled, **not successful file opens**. It is zero when no stream is connected.
Background subscriptions and non-UI clients can also count. Invalid requests
return 422; access and missing-session errors follow the normal session API.

## UI behavior

- A transient `ui.open_file` event reaches the session's current subscribers.
  No request is saved or replayed on reconnect or a later session visit.
- A UI handles it only while displaying that session and permitted to browse
  its workspace. Other sessions are unaffected. In a grid, only the selected
  pane owns the shared workspace viewer and handles requests.
- Web, desktop and mobile layouts use their existing file viewer. The normal
  file tab, panel-opening behavior and unsaved-edit guard apply. A user can
  cancel navigation rather than lose an edit.
- Existence and file-read permissions are checked by the normal filesystem
  API when the viewer loads. Missing, inaccessible or unsupported files show
  the usual viewer error/fallback; the POST does not verify file existence.

## Try it

1. Start this checkout with `omnidev` and open its displayed UI URL.
2. Open a session whose workspace contains `README.md`.
3. Ask the agent: **Use `ui_open_file` to open `README.md` in the UI.** The
   workspace viewer should open without clicking a file link. Alternatively,
   call the HTTP endpoint with that session ID and `README.md` as the path.
4. Switch to another session and repeat the HTTP request for the original
   session. The UI must stay put. Returning to the original session must not
   replay the request.

The automated browser journey is
`tests/e2e_ui/files/test_right_panel.py::test_agent_opens_workspace_file` (desktop
and phone-sized Chromium, mock provider). See the
[verification guide](../feature-map/README.md) for running it in isolation.
