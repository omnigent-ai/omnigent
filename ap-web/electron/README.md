# Omnigent Desktop (Electron)

A thin [Electron](https://www.electronjs.org) desktop shell around the
existing Omnigent web UI. It shows the **same** UI you get in a browser, but
adds native niceties:

- **OS-native desktop notifications** (via the main-process `Notification`
  API) when an agent finishes a turn (`running` → `idle`/`failed`), raises a
  new elicitation (asks for input), or a runner disconnects (`online` →
  `offline`). A notification fires for any such event **except** the one
  conversation you're actively viewing (window focused _and_ that chat
  open). Sessions already settled at launch don't fire; only fresh
  transitions this client observes do. On a turn-end the notification body
  shows the **first few lines of the agent's final message** when they can be
  fetched (one best-effort `GET /items` call), falling back to a generic
  "Agent finished and is ready for your input."
- **A foreground attention cue.** macOS (and Windows) suppress the notification
  _banner_ for the **frontmost** app — the notification still lands in
  Notification Center, but no toast pops, which reads as "notifications only
  work when the app is in the background." Because the web layer already only
  notifies for sessions you are _not_ actively viewing, the shell adds an
  OS-level cue the frontmost app _can_ show: it **bounces the macOS dock icon**
  (or flashes the taskbar frame on Windows/Linux) so an unopened session's
  turn-end is noticeable even with Omnigent in front.
- **Multiple windows** (**Server → New Window**, `Cmd/Ctrl+N`). Each window is
  an independent view, opening on the current window's URL so you can then
  navigate it to a different conversation and watch two side by side. A
  window can also be opened against a **different server** (see "Multiple
  servers" below). Notifications and the dock badge are app-wide (one badge
  for all windows); a notification click focuses the window that fired it.
- **A dock / taskbar badge showing the number of unread sessions** at all
  times (macOS dock badge, Linux Unity launcher count, via
  `app.setBadgeCount`). A session becomes "unread" when it finishes a turn
  or asks for input while you're not actively viewing it, and is cleared the
  moment you view it. Runner disconnects notify but do **not** count toward
  the badge.
- **The standard native menu** (App / Edit / View / Window / Help) built from
  Electron's menu roles, so the usual text-editing shortcuts — Cmd/Ctrl-A,
  C, V, X, Z — work inside the webview's text fields. Our custom actions —
  **New Window**, **New Window on Different Server…**, and
  **Change Server…** — live in a dedicated **Server** submenu.
- **Browser-style file drag-and-drop** works out of the box: Electron does
  not intercept file drops the way Tauri does by default, so dropping an
  image onto a text field reaches the web app's HTML5 drop handler with no
  extra configuration.
- **Microphone permission for voice dictation.** The composer's dictation
  button uses the Web Speech API plus a `getUserMedia` audio stream (the mic
  level meter). Both go through Chromium's permission layer, which in Electron
  asks the _embedder_ (us) rather than showing Chrome's prompt — with no
  handler wired, Chromium denies by default, so `recognition.start()` fails
  instantly with `not-allowed` and the button appears dead. The main process
  now wires `setPermissionRequestHandler` / `setPermissionCheckHandler` to
  grant the audio permissions, and on macOS calls
  `systemPreferences.askForMediaAccess("microphone")` lazily — on the first
  actual mic request (the user clicking dictate), not at app startup — so the
  OS-level mic gate is open too (packaged builds ship
  `NSMicrophoneUsageDescription`).

  > **Caveat — Web Speech may still not transcribe in Electron.** Granting the
  > mic clears the _permission_ gate, but `SpeechRecognition` also depends on
  > Google's cloud speech backend keyed to official Google Chrome builds, which
  > Electron's bundled Chromium does **not** ship. So recognition can still
  > fail (typically a `network` error) even with the mic allowed. The web app
  > degrades gracefully (the button shows "Dictation unavailable" rather than
  > crashing). Fully reliable in-app dictation would require a MediaRecorder
  > capture + a server-side transcription endpoint (e.g. Whisper) wired to the
  > composer's existing `onAudioRecorded` fallback — not yet implemented.

## How it works (zero UI duplication)

The desktop app does **not** ship a copy of the web UI. It bundles only a tiny
"connect to server" page (`setup/index.html`). On launch:

1. If no server URL is saved yet, it shows the setup page (one input +
   Connect). You enter your Omnigent server URL (default
   `http://localhost:8000`).
2. It persists that URL to the per-user app data dir (`settings.json` under
   Electron's `userData` path) and **loads the server's own origin**, where
   the server serves the real SPA (the production `ap-web` build, the same
   bytes a browser would load).
3. On subsequent launches it skips the setup page and loads the saved server
   directly.

If the saved server fails to load (server down, DNS failure, TLS error), the
window falls back to the setup page with the error shown and the failed URL
pre-filled — the saved URL is kept, so Connect simply retries it.

Entering a plain-`http://` URL for a **non-local** host shows a warning first
(anyone on the network path can act as that server); a second Connect click
proceeds. `http://localhost:8000` connects with no friction.

Change the server later via the **Server → Change Server…** menu item, which
clears the saved URL and returns the focused window to the setup page.

Open another view with **Server → New Window** (`Cmd/Ctrl+N`). It clones the
focused window's current URL onto a new window against the same server, so two
conversations can be watched at once.

The native enhancements live on the web side in
[`../src/lib/nativeBridge.ts`](../src/lib/nativeBridge.ts). It detects the
Electron shell at runtime (the preload exposes `window.omnigentDesktop`
with `kind: "electron"`) and routes notifications/badge through the IPC
bridge; in a plain browser it falls back to the Web Notifications path. So the
one `ap-web` bundle works both in a browser and under Electron.

## Architecture

```
electron/
  package.json        # Electron + electron-builder deps and build config
  src/main.js         # main process: window, settings, menu, IPC, badge, notify
  src/preload.js      # contextBridge: window.omnigentDesktop + omnigentSetup
  src/find_preload.js # contextBridge for the find bar: window.omnigentFind
  setup/index.html    # the bundled "connect to server" setup page
  find/index.html     # the bundled find-in-page bar (Cmd/Ctrl+F)
  icons/              # app icons
```

Native niceties beyond notifications/badge: a right-click context menu
(cut/copy/paste, spelling suggestions + Add to Dictionary, Copy Link
Address), window size/position persistence across launches, and
find-in-page (**Edit → Find…**, `Cmd/Ctrl+F`) — a small bar anchored to the
window's top-right corner; Enter / Shift+Enter step through matches, Esc
dismisses.

- **Main process** (`src/main.js`) owns settings persistence, window
  creation, the application menu, permission handling (microphone), and IPC
  handlers for the badge and notifications (`normalize_url`, `change_server`,
  navigate-to-server, New Window).
- **Preload** (`src/preload.js`) is the only bridge between the remote
  (untrusted) SPA and the main process. It runs with `contextIsolation` and
  exposes a tiny, serialization-safe API via `contextBridge` — never raw
  `ipcRenderer` or Node.
- **Security posture**: `nodeIntegration: false`, `contextIsolation: true`.
  `window.open` / `target=_blank` links are opened in the user's real
  browser, not chromeless Electron windows. Non-web schemes (`vscode://`,
  `ssh://`, …) launch an OS protocol handler with page-controlled
  arguments, so they prompt for consent first — showing the requesting
  origin and the full URL — with an optional persisted "always allow this
  scheme from this server". Beyond that, each window is
  **pinned to the one server origin the user explicitly connected it to**,
  and that pin — not navigation — is the trust boundary:
  - Navigation is deliberately _not_ restricted: servers may sit behind
    auth that redirects through external identity providers, so a window
    can legitimately visit foreign origins mid-login.
  - Instead, every privileged IPC handler verifies its sender frame.
    `notify` / `setBadgeCount` only work when both the calling frame _and_
    the window's top-level page are on the pinned origin (so a pinned-origin
    iframe embedded in a hostile page gets nothing); the setup bridge
    (`omnigentSetup`) only works for the bundled setup page itself, so a
    server page can never read or silently re-point the saved server URL.
    Foreign pages get an inert bridge.
  - The microphone permission grant is likewise scoped: only the audio set,
    only for pages on an origin some window is pinned to, and only when the
    requesting page is the top-level page — everything else is denied.

## Prerequisites

- **Node** 22.x + npm (already used by `ap-web`).
- Electron ships its own Chromium/Node, so no system webview libs are needed
  on Linux for _running_ the built app, though packaging tools may pull a few
  build deps.

## Run it (development)

From the `ap-web/electron/` directory:

```bash
npm install     # installs electron + electron-builder
npm start        # launches the Electron shell
```

The shell opens on the bundled setup page. Point it at a running Omnigent
server (see below), Connect, and you're in.

> Note: this loads the UI from whatever server URL you give it — it does
> **not** run the Vite dev server. To develop the web UI itself with hot
> reload, run `npm run dev` (plain Vite in a browser) from `ap-web/` as usual.

## Build a distributable

From `ap-web/electron/`:

```bash
npm run build             # current platform
npm run build:mac         # .dmg + .zip (signed if an identity is available, not notarized)
npm run build:mac:release # .dmg + .zip, signed + notarized (requires credentials, see below)
npm run build:linux       # AppImage + .deb
npm run build:win         # NSIS installer
```

Output lands in `electron/dist/` (the DMG is named
`Omnigent-<version>-<arch>.dmg`).

## macOS code signing & notarization

The mac build is configured for Apple's **hardened runtime** with the
entitlements Electron needs (`build/entitlements.mac.plist`: V8 JIT plus
microphone for dictation). Signing is driven entirely by what credentials
are present — there are no code changes between a dev build and a release
build:

| Credentials present                                                | Result                                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------- |
| none                                                               | ad-hoc–signed app; runs locally, other Macs see a Gatekeeper warning |
| Developer ID cert                                                  | signed app; downloads still warn until notarized                     |
| Developer ID cert + Apple notarization creds (`build:mac:release`) | signed + notarized; installs cleanly everywhere                      |

### 1. Get a signing certificate

You need a **Developer ID Application** certificate from an Apple Developer
Program account (the kind used for distribution _outside_ the App Store).
Create it at <https://developer.apple.com/account/resources/certificates>
(or via Xcode → Settings → Accounts → Manage Certificates), then either:

- **Keychain (local builds):** install the cert + private key into your
  login keychain. electron-builder auto-discovers it — `npm run build:mac`
  just works. Verify with
  `security find-identity -v -p codesigning` (you should see
  `Developer ID Application: <Your Name> (<TEAMID>)`).
- **Env vars (CI):** export the cert + key as a password-protected `.p12`
  and set:

  ```bash
  export CSC_LINK=/path/to/developer-id.p12   # or a base64 string / https URL
  export CSC_KEY_PASSWORD='the p12 password'
  ```

To force an **unsigned** build even when a cert is present (faster dev
iteration): `CSC_IDENTITY_AUTO_DISCOVERY=false npm run build:mac`.

### 2. Notarize (release builds)

Notarization uploads the signed app to Apple for malware scanning;
without it, macOS warns on first launch of a downloaded app. It needs
network access and Apple credentials — either an App Store Connect API
key (preferred for CI):

```bash
export APPLE_API_KEY=/path/to/AuthKey_XXXXXXXXXX.p8
export APPLE_API_KEY_ID=XXXXXXXXXX
export APPLE_API_ISSUER=<issuer-uuid>
```

or your Apple ID with an [app-specific password](https://support.apple.com/102654):

```bash
export APPLE_ID=you@example.com
export APPLE_APP_SPECIFIC_PASSWORD=xxxx-xxxx-xxxx-xxxx
export APPLE_TEAM_ID=<TEAMID>
```

then:

```bash
npm run build:mac:release
```

This is the same build with `mac.notarize=true` switched on; expect the
notarization step to add a few minutes (Apple-side processing). Verify the
result with:

```bash
spctl -a -vv dist/mac-arm64/Omnigent.app   # → "accepted, source=Notarized Developer ID"
```

`build:mac:release` **fails loudly** if signing or notarization
credentials are missing — that's intentional, so a release artifact can't
silently ship unsigned.

## Getting a server to point at

Any reachable Omnigent server works. For a quick local target, run the
server from this repo:

```bash
# from the repo root, with the project venv:
.venv/bin/python -m omnigent.server   # serves on http://localhost:8000
```

Then enter `http://localhost:8000` in the setup page.

## Managing servers and hosting

Beyond pointing at an already-running server, the shell can drive the local
`omnigent` CLI to start a server and register this machine as a **host** (a
machine that runs the agent work a server dispatches). Two concepts stay
deliberately separate:

- **Server** — the backend the webview talks to (local or remote).
- **Host** — _this machine_ executing agent work for a server. Because hosting
  runs agent code, it is **opt-in** and **explicit**: the shell never connects
  this machine as a runner on its own — not on connect, not on launch. You
  connect it from the **host selection menu** inside the app (when starting a
  chat, pick this machine), which drives `controlHost` over the bridge. That
  request alone isn't trusted to authorize hosting: the SPA is served by the
  server, so `start`/`restart` additionally require a **native, main-process
  confirmation** the page can't forge or auto-dismiss (persisted per server
  origin, so a trusted server is asked only once).

### Detecting the CLI (setup page)

On the setup page the shell probes for the `omnigent` binary —
`settings.omnigent_path` first, then `PATH`, then the well-known install
locations (`~/.local/bin`, `~/.cargo/bin`, Homebrew, `/usr/local/bin`). A
GUI-launched app inherits a minimal `PATH`, which is why the install locations
are probed directly. When the CLI isn't found, the page shows the install
one-liner

```bash
curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh
```

and a field to point the app at the binary (typed or via a native file picker).
A configured path is saved to `settings.json` (`omnigent_path`) only once it
validates as a runnable `omnigent`. Connecting to a **remote** server never
needs the CLI — only "Start locally" and hosting do.

### Start locally

**"Start a server on this machine"** runs `omnigent server start` (idempotent —
reuses a healthy one) and then connects this window to its
`http://127.0.0.1:<port>` URL through the normal connect flow. It does not
connect this machine as a runner — that stays an explicit step in the app.

### Connecting this machine as a runner

There is **no** connect-time toggle and no sidebar status row: the shell never
connects a runner automatically. Inside the connected app, the host selection
menu (when starting a chat) tags this machine and offers to connect it. Choosing
it calls `controlHost("start")` over the bridge. Because that call originates in
server-served code, the main process does not treat it as the user's consent: on
the first `start`/`restart` for a server origin it shows a **native confirmation
dialog** ("Allow _host_ to manage Omnigent on this machine?") with **Don't Allow**
(default) / **Allow Once** / **Always Allow**. Only after approval does it — once
the CLI is authenticated for the server (remote only; local needs none) — either
adopt a daemon already serving that server (one you started by hand) or spawn
`omnigent host --server <url>`. **Allow Once** connects this time and re-prompts
next time; **Always Allow** records the origin in `settings.json`
(`allowed_hosting_origins`) so later connects skip the prompt. `stop` is
fail-safe and needs no confirmation. The same bridge exposes `stop` / `restart`.

Status is read live (host connected = a live daemon process **and** an online
host tunnel; the shell never caches it). The host surface goes through the JS
bridge — `window.omnigentDesktop` → `getHostStatus` / `getHostIdentity` /
`onHostStatusChanged` (read + live) and `controlHost` (start/stop/restart),
typed in [`../src/lib/nativeBridge.ts`](../src/lib/nativeBridge.ts) and gated to
the window's **pinned origin** like the badge/notification bridge.

### Lifecycle

The desktop **owns the host processes it starts**: quitting the app SIGTERMs
them (and stops a local server it started), so closing the app disconnects this
machine. A daemon the shell merely _adopted_ (you started it in a terminal) is
left running on quit. Hosting is **not** restored on the next launch — you
reconnect this machine explicitly from the host menu when you want it.

## Passkeys (WebAuthn)

External security keys (e.g. a YubiKey) work out of the box: Chromium's
content layer speaks CTAP to the key directly. That's also why the flow is
_invisible_ — the passkey sheet you see in Chrome/Safari is browser chrome,
which Electron doesn't ship. Touching the key completes the ceremony with no
UI.

For a visual flow, the shell enables Electron's **Touch ID platform
authenticator** (`app.configureWebAuthn`, Electron ≥ 42, macOS only):
registering or signing in with a platform passkey then shows the native
macOS Touch ID / keychain dialog, and a native chooser appears when several
saved passkeys match. Three pieces must agree before this activates:

1. `WEBAUTHN_KEYCHAIN_ACCESS_GROUP` in `src/main.js` —
   `"<TEAM_ID>.ai.omnigent.desktop"`.
2. The same string in the `keychain-access-groups` entitlement in
   `signing/entitlements.mac.plist`.
3. An **embedded Developer ID provisioning profile**
   (`signing/omnigent.provisionprofile`, wired via `provisioningProfile`
   in `package.json`). `keychain-access-groups` is a _restricted_
   entitlement: a Developer ID signature alone doesn't authorize it, and
   AMFI SIGKILLs the app at launch ("Launchd job spawn failed", POSIX
   error 163). Create the profile in the Apple Developer portal: an App ID
   for `ai.omnigent.desktop` (no extra capabilities — every profile
   automatically authorizes keychain groups under `<TEAM_ID>.*`), then
   Profiles → Distribution → Developer ID for that App ID. Verify with
   `security cms -D -i signing/omnigent.provisionprofile`.

The signing identity's team must match the group prefix —
`package.json` pins `"identity"` for this reason (with several certs in
the keychain, electron-builder's auto-discovery can pick the wrong one).
Helpers must NOT inherit the keychain entitlement
(`entitlementsInherit` points at the minimal
`signing/entitlements.mac.inherit.plist`; a restricted entitlement on a
helper shows up as a "GPU process exited unexpectedly" crash loop).

It only works in a **code-signed** build, on Macs with a Secure Enclave.
Until all three are set — and always in unsigned `npm start` dev runs —
the platform authenticator stays off and security keys remain the
(working, silent) path.

Caveats: these passkeys are device-bound in the app's own keychain access
group — they are **not** synced via iCloud Keychain, and passkeys you saved
in Safari/Chrome are not visible to the app (and vice versa). Showing the
full system passkey sheet (iCloud Keychain, cross-device QR) for arbitrary
user-chosen servers would require Apple's browser-only
`web-browser.public-key-credential` entitlement, or per-domain associated
domains — neither fits an app whose servers are user-deployed.

## Localhost access (auth flows)

Trusted pages may call services on the user's own machine
(`http://localhost:<port>`, `127.0.0.1`, `[::1]`) even when those
services don't send CORS headers — authentication flows use this to
reach local auth helpers/token brokers. The shell injects the CORS (and
preflight) response headers itself, scoped to requests _from_ a trusted
page origin _to_ a loopback host; see `src/localhost_cors.js`. Trusted
means:

- a window's **pinned server origin**, or
- the **current top-level page of a pinned window** — auth flows
  redirect the main frame through SSO/IdP origins that can't be known in
  advance (server → SSO domain → localhost helper probe), and those
  pages get localhost access while the user is actually on them.
  In-window navigation only starts from the pinned server (links/popups
  open in the external browser), so this doesn't extend to arbitrary
  sites; iframes never match (main-frame origin only).

Anything else stays blocked by normal CORS, and a localhost service that
sends its own `Access-Control-Allow-Origin` keeps enforcing its own
policy untouched.

If a page needs localhost while _not_ being the visible top-level page,
hand-add its origin to `settings.json`:

```json
{ "localhost_allowed_origins": ["https://login.example.com"] }
```

(`settings.json` lives in Electron's per-user `userData` dir — on macOS,
`~/Library/Application Support/Omnigent/settings.json`.)

## Multiple servers

One server URL is saved as the default, but extra windows can be opened
against _different_ servers via **Server → New Window on Different
Server…**. It opens a setup page in **per-window** mode: the URL you connect
applies to that window only and is never saved, so the default server is
untouched and the extra connection ends when the window closes. These
windows get the same per-window origin pinning as regular ones. With windows
on more than one server, the dock badge shows the sum of each server's unread
count and notification titles are prefixed with the firing server's hostname.

## Implementation notes

- **Runtime:** bundled Chromium (so the build is ~100+ MB, but the renderer
  matches Chrome's behavior exactly — no OS-webview quirks).
- **Native bridge detection:** `window.omnigentDesktop` (`kind: "electron"`),
  exposed by the preload. The web-side `nativeBridge.ts` routes the badge to
  `app.setBadgeCount` and notifications to the main-process `Notification` API
  via IPC; in a plain browser it falls back to the Web Notifications path.
- **File drag-drop** works by default (Electron doesn't intercept HTML5 file
  drops).
- **Toolchain:** Node only — no Rust or platform webview libraries.

> Historical note: an earlier Tauri-based shell lived in `ap-web/src-tauri`.
> It was removed in favor of shipping Electron only; `nativeBridge.ts` no
> longer carries a Tauri code path.
