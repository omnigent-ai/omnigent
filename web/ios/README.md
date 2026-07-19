# Omnigent iOS

Thin SwiftUI/WKWebView shell for Omnigent. Like the Electron app, this target
loads the server-served web UI instead of shipping a duplicate copy of the SPA.

## Development

Open `Omnigent.xcodeproj` in Xcode 16 or newer and run the `Omnigent` scheme on
an iOS 18 simulator.

Debug builds allow `http://` web content for local development by enabling
`NSAllowsArbitraryLoadsInWebContent`. Release builds keep App Transport
Security defaults and require remote servers to use `https://`.

## Scope

The first version provides native setup chrome, recent servers, WKWebView
loading, foreground local notifications, app badge updates, and notification
tap routing back into the SPA. It does not implement APNs, background polling,
or localhost proxy/CORS behavior.

## Deep links

An `omnigent://<hostname>/c/<session_id>` URL opens that session on that server
in the app, mirroring the Electron desktop shell (see
`designs/desktop-deep-link.md` for the shared design):

```
omnigent://localhost:8000/c/conv_abc              → http://localhost:8000/c/conv_abc
omnigent://my-workspace.cloud.databricks.com/c/x → https://…/ml/omnigents/c/x
```

The link names a server by **host** (with port if non-default) and carries no
`http`/`https`; the scheme is inferred with the same rule as the setup page
(`http` for loopback, `https` for a remote host), so a deep link and a pasted
URL never disagree. The Databricks workspace mount (`/ml/omnigents`) is **not**
in the link; it is discovered by `WorkspaceURLExpander`. v1 accepts only
`/c/<session_id>`.

**Handling** (single window, unlike the desktop's multi-window):

- If the app is already on the link's server, the SPA router navigates
  **in-place** (no reload) — the path is deferred until the page finishes
  loading, so a cold-start link to the saved server isn't lost.
- A **known** server (in recents or the saved default) the app isn't currently
  on is switched to, loading the conversation directly; no prompt.
- A **never-connected** server prompts with a native confirmation — pinning a
  new origin is a privilege grant (notifications, badge, mic), so a clicked
  link must not silently connect to an attacker-chosen server. The workspace
  probe runs only **after** consent, so a link to an unknown host makes no
  pre-consent network request.

The conversation path never enters the saved server URL or recents (only the
load URL carries it), so a later deep link resolves against a clean server
identity. The scheme is registered via `CFBundleURLSchemes` in both Info plists;
test from the simulator with `xcrun simctl openurl booted 'omnigent://...'`.
The web UI must be rebuilt (`cd web && npm run build`) for the SPA's `onOpenPath`
subscriber to be present in the served bundle.
