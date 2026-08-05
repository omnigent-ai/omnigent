# Android `omnigent://` deep-link handling — design

Register the Android shell (`web/android`) as a handler for
`omnigent://<host>[:port]/c/<session_id>` links and handle them with the same
user-visible semantics as the iOS and desktop shells, using idiomatic Android
mechanisms. Behavior (URL shape, scheme inference, consent rules) stays
consistent across shells; the implementation follows Android platform patterns
rather than mirroring iOS structure.

## Prerequisite: OIDC login/origin binding (separate precursor PR)

The Android shell has a pre-existing race, reachable today via the
server-switcher menu: `OidcLoginManager` polls the *old* origin for a token,
but `onSessionToken` injects it into whatever `pinnedOrigin` is current, and
`reloadWithNewServer` neither cancels nor invalidates an in-flight login. A
server switch mid-login can therefore place one server's session token on
another origin. Deep links make this externally triggerable, so it must be
fixed **before** this feature lands, as its own PR:

- Bind every login result to the origin that started it; `onSessionToken`
  drops a token whose originating origin no longer matches `pinnedOrigin`.
- `reloadWithNewServer` cancels any in-flight login flow.
- Regression test: token arriving after a server switch is rejected.

This spec assumes that fix is in place.

## Link contract (shared with iOS/desktop)

- Shape: `omnigent://<host>[:port]/c/<id>` — nothing else is accepted.
- The link carries no `http`/`https`; the scheme is inferred: `http` for
  loopback hosts (`localhost`, `127.0.0.1`, `::1`), `https` otherwise.
- The Databricks workspace mount (`/ml/omnigents`) is never in the link; it is
  server-determined.
- Custom schemes are unverified on Android exactly as on iOS: any co-installed
  app may also declare `omnigent` and receive the link (host + conversation id
  are metadata-disclosed; the id carries no secret). Treat the scheme as
  untrusted input.

### Threat model for the new exported surface

Adding `BROWSABLE` lets any app or web page fire VIEW intents at the exported
`MainActivity`. Mitigations, all part of this design:

- No pre-consent network request or persistence for unknown origins; known
  and same-origin links only navigate within already-trusted servers.
- `MainActivity` keeps the default task affinity (none is declared today);
  no `singleInstance`/`singleTask` change, so task hijacking surface is
  unchanged.
- Rejected links are ignored without feedback; debug-only logging never
  prints the full untrusted URI (host + rejection reason at most).
- Repeated link spam degrades to repeated consent dialogs at worst (see FIFO
  queue below — one dialog at a time, each replacing none).

Verified HTTPS Android App Links for operator-controlled Databricks domains
(pinned via `assetlinks.json`, immune to scheme squatting) are the better
transport where available, but need coordinated HTTPS link emission and
server-side `assetlinks.json` deployment — a dedicated follow-up, mirroring
the iOS Universal Links stance. The custom scheme remains for BYO/self-hosted
servers.

## Components

### 1. Manifest registration

An `<intent-filter>` on `MainActivity` (which already uses
`launchMode="singleTop"`):

- `android.intent.action.VIEW`
- categories `DEFAULT` + `BROWSABLE` (required for links tapped in a browser)
- `<data android:scheme="omnigent" />`

Device testing found this filter can't live on `MainActivity` itself:
Android's task-fronting silently swallows a VIEW intent when another
activity (e.g. `ConnectActivity`) is on top of the same task. The filter
instead lives on `DeepLinkActivity`, a `noHistory` trampoline that always
forwards to `MainActivity` via `FLAG_ACTIVITY_CLEAR_TOP |
FLAG_ACTIVITY_SINGLE_TOP` and finishes.

Intent delivery is *not* assumed to arrive only via `onNewIntent`:
`singleTop` routes there only when this instance is top-of-task. Cold starts,
launches while `ConnectActivity` is on top, and launches while the external
login browser is foreground all enter `onCreate` of a (possibly new)
`MainActivity` instance. Both entry points funnel into the same handler (see
routing below), which is idempotent per intent.

### 2. `DeepLink.kt` — pure parser

A small value type + `parse(uri: Uri): DeepLink?` in `ai.omnigent.android`,
producing `(origin, path)`. The accepted grammar is complete — anything not
matching returns null (an unrecognized link must never crash or mis-navigate):

- Hierarchical URI, scheme `omnigent` (case-insensitive), non-empty host.
- **No userinfo, no query, no fragment** — a link carrying any of them is
  rejected outright (`Uri.getUserInfo()`/`getQuery()`/`getFragment()` checked
  explicitly; `Uri.getPath()` alone would silently drop them). This is
  deliberately stricter than iOS/desktop, which drop these parts silently;
  our own link emitters (SPA, QR) never produce them.
- Port, when present, must be a valid 1–65535 integer.
- Bounded input: links longer than 2048 characters are rejected.
- Path of exactly `/c/<id>` (one optional trailing slash tolerated). The id
  segment is validated against a **denylist**, not a grammar (the SPA's
  router stays the authority on id format): rejects `?`, `#`, `/`, `.`, `%`,
  and control characters (U+0000–U+001F, U+007F). `Uri.getPath()` is
  percent-decoded, so smuggled encoded separators (`%3F`, `%23`, `%2F`,
  `%2E`, `%00`) reappear as literals and are caught; a malformed escape
  leaves a stray `%`, also caught.
- Origin construction: infer `http` for loopback (`localhost`, `127.0.0.1`,
  `::1`), `https` otherwise; re-bracket IPv6 hosts; then canonicalize through
  the **same `originOf` path** used by the bridge allowlist and navigation
  gate (lowercased host, default ports omitted) so a deep-link origin
  compares equal to a pinned/stored origin by construction. `originOf` gains
  IPv6 re-bracketing as part of this work (it currently drops brackets),
  with tests that existing callers are unaffected. Unicode hosts are
  IDNA/punycode-normalized before comparison; if the platform parse cannot
  produce a canonical ASCII host, the link is rejected.

### 3. Routing in `MainActivity`

`onCreate` and `onNewIntent` both pass `ACTION_VIEW` intents with a data URI
to one handler. Parsed links enter a small **FIFO queue** processed one at a
time (desktop precedent): a link is dequeued only when the previous one has
fully resolved (navigated, or consent answered). Origin and path travel
together as one queued value — the consent dialog and the pending navigation
can never disagree about which link they belong to. At most one consent
dialog is ever showing.

Per dequeued link:

- **Same origin as pinned** → set the pending navigate path (existing
  notification-activation replay) and flush; the SPA navigates in place with
  no reload.
- **Known server** — a `ServerStore` recent whose `originOf(url)` matches the
  link origin (this also covers stored URLs that carry a workspace mount) →
  `store.connect(storedUrl)` + `reloadWithNewServer(...)`, path left pending
  so `onPageReady` flushes it. No consent (the user already chose this
  server).
- **Unknown server** → AppCompat `AlertDialog` consent before any network
  request or persistence: "This link will connect Omnigent to `<host>` and
  open a conversation."
  - **Open** → load the inferred origin and leave the path pending.
    **Persistence is deferred**: `ServerStore.connect` is called only from
    the first successful pinned-origin `onPageReady` for that origin (iOS
    behavior) — an unreachable or hostile host never becomes a trusted
    recent. Until that load succeeds the previous server remains the stored
    current server.
  - **Cancel** → drop the link; app state unchanged (or continue to
    `ConnectActivity` when no server was ever configured).
- **Cold start with no server configured + deep link** → the consent flow
  runs instead of the unconditional `ConnectActivity` redirect (dialog over
  an empty activity is acceptable; Cancel routes to `ConnectActivity`).

### 4. Back semantics (decision table)

| Case | Back after deep link does |
|---|---|
| Warm, same origin (in-place SPA navigation) | SPA history: returns to the previous in-app screen, as after any SPA navigation |
| Warm, server switch (known or consented) | Clean stack on the new server (existing `reloadWithNewServer` + `clearHistory` behavior): Back exits the app |
| Cold start onto any server | Clean stack: Back exits the app |
| Consent dialog showing | Back dismisses the dialog = Cancel; app state unchanged |

No new history mechanism; this documents what the existing machinery already
produces and the tests pin it.

### 5. Network policy: loopback in release

The release `network_security_config` currently denies all cleartext, which
would make valid loopback deep links parse but fail to load. Add a narrowly
scoped cleartext exemption for the accepted loopback hosts (`localhost`,
`127.0.0.1`, `::1`) to the **release** config, matching iOS ATS (which
exempts loopback even in release). Loopback cleartext never leaves the
device. The emulator host alias `10.0.2.2` stays debug-only.

### 6. Strings

Consent dialog title/body/buttons in `res/values/strings.xml`, matching the
iOS prompt copy.

## Error handling and recovery

- Unparseable/rejected links are ignored; debug builds may log host +
  rejection reason, never the full untrusted URI. A malformed cold-start
  link opens the app normally with no error surface.
- **Post-consent load failure** (DNS/TLS/HTTP error): the failed origin is
  never persisted (persistence is load-success-gated). A DNS/TLS failure keeps
  its path queued for the next successful retry, while the WebView shows its
  error page with the server switcher available as the recovery path.
- **Pending-path hygiene**: a pending deep-link path is bound to its target
  origin; it flushes only on a pinned-origin `onPageReady` for that origin
  and is discarded when a later server switch or newer link supersedes it —
  it cannot linger indefinitely and fire against the wrong server.
- A link arriving while the WebView is parked off-origin (mid-login) queues;
  a same-origin link then flushes on the next pinned-origin `onPageReady`
  (existing `flushPendingActivation` behavior). A server-switching link is
  handled once dequeued — safe because the precursor PR makes login results
  origin-bound.
- Deep link to a conversation that doesn't exist: the SPA owns the failure
  (its `/c/:id` route renders its own not-found state); the shell does not
  pre-validate ids.

## Known gap (documented, out of scope)

Android has no `WorkspaceURLExpander`. A consent-approved **unknown**
Databricks workspace host connects to the bare origin without probing the
`/ml/omnigents` mount, so such links only fully work for servers the user has
already connected to (their stored URL keeps the mount). **This PR adds the
limitation and follow-up note to `web/android/README.md`** alongside the
existing deep-link docs.

## Testing

**Parser** — `DeepLinkTest.kt` under `app/src/test` (Robolectric, since the
parser uses `android.net.Uri`), covering the iOS `DeepLinkTests.swift` cases
plus the stricter grammar:

- Valid: loopback (http inferred), remote host (https inferred), explicit
  non-default ports, IPv6 loopback, trailing slash on the id, uppercase
  scheme/host (canonicalized).
- Rejected: wrong scheme, missing host, non-`/c/` paths, empty/nested ids,
  literal and percent-encoded `?` `#` `/` `.`, `%zz` malformed escapes,
  control characters, path traversal (`..`), **real query strings, fragments,
  and userinfo**, invalid ports, over-length input.
- Canonicalization: explicit `:443`/`:80` dropped, host lowercased, deep-link
  origin equals `originOf` of the equivalent stored URL (including IPv6).

**Routing** — Robolectric activity/intent tests:

- Cold start via VIEW intent: no-server → consent; known server → switch +
  navigate; same saved server → navigate.
- Warm `onNewIntent`: same-origin navigation without reload; known-server
  switch; unknown-server consent accept and cancel.
- Persistence: unknown server absent from `ServerStore` until a successful
  pinned-origin page load; absent after a failed load.
- FIFO: two links arriving back-to-back resolve in order; a second unknown
  link waits for the first consent to be answered; origin/path pairing holds.
- Pending-path hygiene: path dropped when superseded; not flushed against a
  different origin.

**Manual/device** — `adb shell am start -a android.intent.action.VIEW -d
"omnigent://<host>/c/<id>"` against cold start, warm same-origin,
known-server switch, unknown-server consent (accept and cancel), link while
`ConnectActivity` is on top, link during an in-flight browser login, and a
release build loading a `localhost` link (cleartext exemption).
