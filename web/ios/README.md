# Omnigent iOS

Thin SwiftUI/WKWebView shell for Omnigent. Like the Electron app, this target
loads the server-served web UI instead of shipping a duplicate copy of the SPA.

## Development

Open `Omnigent.xcodeproj` in Xcode 26 or newer and run the `Omnigent` scheme on
an iOS 26 simulator.

Debug builds allow `http://` web content for local development by enabling
`NSAllowsArbitraryLoadsInWebContent`. Release builds keep App Transport
Security defaults and require remote servers to use `https://`.

## Scope

The first version provides native setup chrome, recent servers, WKWebView
loading, foreground local notifications, app badge updates, and notification
tap routing back into the SPA. It does not implement APNs, background polling,
or localhost proxy/CORS behavior.

## Managed app configuration

An administrator can preset the server URLs the app offers via a managed app
configuration, so managed users pick their organization's server from the connect
screen instead of typing a URL. See
[`docs/managed-app-configuration.md`](docs/managed-app-configuration.md) for the
published specification (keys, error codes, sample payload) — that document is
what administrators read, so keep it in sync with `ManagedConfiguration.swift`.

Two delivery channels are supported: a `com.apple.configuration.app.managed`
declaration (preferred — validated, with errors reported back to the admin) and
the classic `com.apple.configuration.managed` defaults key (any MDM, no error
reporting). The declarative one wins when both are present.

Preset servers are offered, not enforced: nothing connects automatically, the
user can still type any URL, and preset entries are never persisted into recent
servers, so withdrawing the configuration withdraws them from the app.

The real payload arrives over declarative device management, which the Simulator
cannot do at all — it has no MDM enrollment and `simctl` has no profile-install
command. To exercise the UI, pass the DEBUG-only launch argument:

```
--omnigent-managed-servers https://one.example.com,https://two.example.com
```

Set it in the scheme's arguments (Product › Scheme › Edit Scheme › Run ›
Arguments) for a manual run; `ManagedServersUITests` uses the same seam. To drive
it from the command line, ask `xcodebuild` where the app is rather than guessing
a DerivedData path — there are usually several, and installing a stale one looks
exactly like the feature not working:

````sh
cd web/ios
DEVICE='platform=iOS Simulator,name=iPhone 17,OS=26.5'
xcodebuild build -project Omnigent.xcodeproj -scheme Omnigent -destination "$DEVICE" -quiet
APP="$(xcodebuild -project Omnigent.xcodeproj -scheme Omnigent -destination "$DEVICE" \
  -showBuildSettings | awk -F' = ' '/ BUILT_PRODUCTS_DIR/{print $2; exit}')/Omnigent.app"

xcrun simctl boot 'iPhone 17' 2>/dev/null
xcrun simctl install booted "$APP"
xcrun simctl launch booted ai.omnigent.ios --omnigent-reset-state \
  --omnigent-managed-servers 'https://omnigent.corp.example.com,https://my-workspace.cloud.databricks.com/ml/omnigents'
```

To exercise the real configuration plumbing instead of a test seam, push a classic
configuration into the app's own defaults — this is the same key an MDM writes, so
nothing about the app's code path is faked:

```sh
xcrun simctl spawn booted defaults write ai.omnigent.ios com.apple.configuration.managed \
  '{ serverUrls = ("https://omnigent.corp.example.com", "https://my-workspace.cloud.databricks.com/ml/omnigents"); }'
xcrun simctl terminate booted ai.omnigent.ios
xcrun simctl launch booted ai.omnigent.ios

# Simulate an administrator changing it later: rewrite the key, then leave and
# re-enter the app. Out-of-process writes don't notify the app, so the list is
# re-read when it next becomes active.
xcrun simctl spawn booted defaults delete ai.omnigent.ios com.apple.configuration.managed
```

The declarative channel cannot be exercised this way; only a real enrolled device
can deliver a declaration. Decoding and validation are covered without any device management
by `ManagedConfigurationTests`. Verifying that a real configuration is delivered,
and that a bad value reports back to the admin console, requires a supervised
device enrolled in an MDM that supports declarative app configuration.

## Deep links

An `omnigent://<hostname>/c/<session_id>` URL opens that session on that server
in the app, mirroring the Electron desktop shell (see
`designs/desktop-deep-link.md` for the shared design):

````

omnigent://localhost:8000/c/conv_abc → http://localhost:8000/c/conv_abc
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
The web UI must be rebuilt (`pnpm --filter web run build`) for the SPA's `onOpenPath`
subscriber to be present in the served bundle.
```
