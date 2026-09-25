# Databricks OAuth on iOS

Workspace-hosted Omnigent uses native OAuth, Keychain credentials, and session-cookie
bootstrap before loading its WebView. Each workspace context has an isolated,
persistent WebKit data store. Databricks Apps keep inline platform sign-in, and
generic OIDC is unchanged.

A workspace connection requires the build configuration and HTTPS callback
association below. Missing configuration returns to setup with an error; it does
not fall back to inline workspace login. Expired sessions get bounded silent
recovery; explicit sign-out clears only the selected workspace's local state.

## Build configuration

Set these user-defined build settings on the **Omnigent** target in Xcode, or
pass them to `xcodebuild`. Both Debug and Release expose them in the processed
Info.plist.

| Build setting                   | Default                                        | Purpose                                                                                                           |
| ------------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `DATABRICKS_OAUTH_CLIENT_ID`    | Empty                                          | Your registered public OAuth client ID. Empty or unresolved values are rejected when native sign-in is requested. |
| `DATABRICKS_OAUTH_REDIRECT_URL` | `https://login.databricks.com/mobile-redirect` | The exact HTTPS callback registered with that client.                                                             |

These are public configuration, not secrets. Never configure a client secret in
the app or borrow the Databricks CLI client ID. The redirect must have a path,
use HTTPS on the default port, and contain no credentials, query, or fragment.

For example, from `web/ios`:

```sh
xcodebuild build -project Omnigent.xcodeproj -scheme Omnigent \
  -configuration Debug -destination 'platform=iOS Simulator,name=iPhone 17 Pro Max,OS=26.5' \
  -derivedDataPath /tmp/omnigent-oauth-config-check \
  DATABRICKS_OAUTH_CLIENT_ID=your-public-client-id \
  DATABRICKS_OAUTH_REDIRECT_URL=https://login.databricks.com/mobile-redirect

plutil -extract DatabricksOAuthClientID raw \
  /tmp/omnigent-oauth-config-check/Build/Products/Debug-iphonesimulator/Omnigent.app/Info.plist
plutil -extract DatabricksOAuthRedirectURL raw \
  /tmp/omnigent-oauth-config-check/Build/Products/Debug-iphonesimulator/Omnigent.app/Info.plist
```

A shell environment variable alone is not an Xcode build-setting override. Pass
settings explicitly to the build command, or set them in the target's build
configuration. This also applies when using a build wrapper such as Fastlane.

## HTTPS callback association

The browser uses `ASWebAuthenticationSession.Callback.https(host:path:)`, not
`callbackURLScheme: "https"` or the `omnigent://` conversation-link handler.

The app's `Omnigent/Omnigent.entitlements` declares
`webcredentials:login.databricks.com` for the default redirect. Before real
sign-in can complete:

1. Enable Associated Domains for the signing App ID/provisioning profile.
2. The callback domain owner must serve an `apple-app-site-association` file at
   `https://<callback-host>/.well-known/apple-app-site-association`, over HTTPS
   without redirects, authorizing the app's application-identifier prefix and
   bundle ID under `webcredentials.apps`.
3. Register the exact redirect URL on the Databricks public OAuth client and
   enable that client for the intended Databricks accounts.

Verify the domain association separately for every shipped bundle ID. This
repository cannot provision the callback domain's association. Its default URL is **not proof that domain
association or OAuth registration has been provisioned**.

If you override the redirect **host**, also change the Associated Domains entry
in the target's entitlements (or supply your own `CODE_SIGN_ENTITLEMENTS` file)
and provision the new domain's AASA file. Overriding only the path on an already
associated host requires updating the OAuth registration, not the entitlement.
Changing the build setting alone does not establish a domain association.

## Native flow

`DatabricksLoginManager.signIn` takes a workspace URL, validated build
configuration, and the presenting window. It stores the access/refresh token bundle
and expiry in Keychain before returning success. Callers must not log tokens or
send them to JavaScript. A cleared or superseded sign-in cannot commit its result.

- Fresh cryptographic state and S256 PKCE verifier for every attempt.
- Start at the entered host's `/oidc/v1/authorize`, with `all-apis offline_access`.
  Forward a supplied `o` value to retain the requested workspace context; do not
  copy unrelated page query parameters into OAuth requests. A canonical workspace
  host can authorize without `o`. No workspace picker or workspace-list API is used.
- Validate the exact callback destination, state, code, and optional `iss` before
  exchanging the code. A missing issuer falls back to the entered origin's `/oidc`
  issuer; a present malformed or duplicate issuer is rejected, not ignored.
- Native, form-encoded token POST in a cookie/cache/credential-isolated session;
  no HTTP redirects, even within the same origin.
- One sign-in at a time, cancellation of browser and token exchange, and rejection
  of duplicate or stale callbacks. Provider error descriptions are not surfaced.
- Normal browser SSO rather than forced ephemeral browsing.

## Issuer discovery

The OAuth authority is distinct from the page destination. Supported issuer shapes
are HTTPS Databricks workspace `/oidc` and account `/oidc/accounts/<account-id>` URLs
on the existing Databricks workspace domain families. Issuers with userinfo,
nondefault ports, queries, fragments, unsupported paths, or outside hosts are rejected.

Before code exchange, this client fetches
`<issuer>/.well-known/openid-configuration` through its isolated transport. Its
validation policy requires an exact issuer match and a `token_endpoint` equal to
`<issuer>/v1/token`. Discovery requests contain no credentials and do not follow
redirects. Inconsistent metadata prevents the token POST; the client does not fall
back to an origin-only endpoint after discovery fails. These describe client
behavior, not a guarantee that every deployment exposes these endpoints.

Consequently, an account issuer keeps its account path for token requests, while a
workspace issuer uses `/oidc/v1/token`. Persist the verified issuer with the opaque
token bundle and retain it on refresh; no JWT decoding is needed for routing. This
metadata is not proof that the grant can access a particular workspace—the platform
must authorize the eventual workspace request. Discovery does not repin the WebView
or change the user's destination.

## Credentials and refresh

Use `DatabricksTokenManager.shared` for all production callers. A
`DatabricksCredentialScope` identifies one account per normalized entered origin,
optional `o`, and exact client ID. The workspace ID is a nonempty ASCII decimal
string; duplicate or malformed `o` parameters are rejected. IDs are not converted
to floating-point numbers. Different IDs on a shared host have separate saved
grants, refresh operations, and clearing boundaries. Paths, host case, and explicit
port 443 do not create separate identities.

The page origin need not equal the saved OAuth issuer. Aliases are not automatically
merged, and account grants are not copied into other workspace records. The original
no-`o` Keychain key format is preserved; a URL with `o` never falls back to an old
ambiguous origin-only record. Version-1 workspace-only records remain readable and
use their legacy refresh route. Issuer-aware records use version 2 so older builds
reject them rather than ignore their routing context. Malformed or unknown record
versions are not silently deleted.

- `tokens(for:)` loads a saved bundle and returns it if it has more than 60 seconds
  remaining. Otherwise, callers for the same scope share one refresh request.
- Refresh uses a form-encoded public-client `refresh_token` grant. A replacement
  refresh token is saved with the entire bundle before callers receive success.
  If the response omits it, the previous refresh token is retained. Issuer metadata
  is retained with the replacement bundle, including across pending-write retries.
- Only a validated HTTP 400 `invalid_grant` response from the expected token
  endpoint automatically clears that scope and returns `nil` (sign-in needed).
  Missing credentials also return `nil`. Network, throttling, server, configuration,
  malformed-response, and Keychain errors do not silently erase credentials.
- Cancelling a waiter stops that caller promptly but lets an issued refresh finish
  and persist, even if no waiters remain. Clearing credentials or committing a newer
  login invalidates the old operation, so late results cannot overwrite or delete
  the newer state. There are no timers or background polling.
- `refresh(rejected:for:)` can renew an unexpired access token rejected during
  cookie bootstrap. It refuses to refresh a newer, unrelated credential snapshot.
- `clear(for:)` is the native credential primitive. Use `DatabricksSignOutManager`
  for complete local sign-out, including ordered web-data cleanup.

Keychain storage uses a narrowly scoped generic-password item,
`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`, and no synchronization or shared
access group. The full versioned token bundle is updated in place, not deleted and
re-added during rotation. There is no UserDefaults or file fallback. These items
cannot migrate to another device through backup/restore; device-only accessibility
is not a claim that they are absent from every possible backup.

If a Keychain write fails after the server rotates a grant, the manager retains
the replacement in memory and retries **persistence first** on the next lookup.
It does not reuse the consumed grant or report durable success. Failed deletions
are likewise retried before any saved token can be returned. These pending changes
are process-local: an app exit or a lost refresh response can still require a new
sign-in, because server-side rotation and local persistence cannot be atomic.

## Session-cookie bootstrap and web-data isolation

`DatabricksWorkspaceBootstrap` first obtains a saved/refreshed grant, or requests
native sign-in. When new sign-in is required, it clears the selected context's web
data **before** a replacement grant can be saved. This avoids pairing new credentials
with old account data if later session creation fails or is canceled.

The session client issues a native `GET /auth/session/create` with a relative
`next_url` containing the intended app path and query. A workspace issuer supplies
the initial exchange origin; account/legacy grants use the entered workspace origin.
An account-issued grant without an explicit workspace ID is rejected rather than
letting an unspecified default workspace share the same persistent store. Enter a
workspace-specific URL instead; no picker or workspace discovery is performed.
The bearer is sent on the first request only, not forwarded through redirects.

Redirects are handled explicitly, with a limit of eight hops. They must stay on
HTTPS Databricks workspace-domain families, with no userinfo, nondefault ports, or
conflicting workspace IDs. A per-operation cookie jar applies the returned cookies
to subsequent requests according to their domain, path, and expiry; it never imports
an existing browser or global URLSession cookie jar. The final app URL retains the
requested conversation/query, not authentication parameters from intermediate URLs.

The client requires a nonempty, unexpired `DBAUTH` cookie valid for the final page.
Live session cookies must have Secure and HttpOnly attributes. Cookie domains must
be valid for the response that set them; domains are never broadened to bridge
hosts. Attributes, companion cookies, and explicit deletions are passed to WebKit.
Unsupported redirects, cookie scopes, login-page landings, and HTTP failures return
to setup rather than loading a different workspace or silently retrying sign-in.
These are client validation rules; verify the deployment's actual cookie-setting
redirect chain during integration.

`DatabricksWebStore` uses a stable, named `WKWebsiteDataStore` for the credential
context: entered origin, optional `o`, and client ID. Different contexts have separate
cookies and other web storage. New native navigation URLs recreate the WebView while
retaining that context's store. Supported canonical/alias redirects stay inside the
same logical store; the native bridge trusts only its active approved page origin.
No old shared-default-store cookies are copied into a workspace store. Other server
authentication modes retain their existing store behavior.

Cookie updates are serialized per store, even across canceled/recreated WebViews.
Before applying a new session, conflicting old cookie names are removed. The caller
waits for writes and reads back the session cookie before loading the page, and checks
that the grant is still current. Canceled/stale startup work cannot load another
context's WebView. The connecting screen offers cancellation and the native server
menu distinguishes workspace IDs on a shared host. Cancellation returns quietly to
setup with the entered URL preserved; configuration and session failures still show
an error.

## Session recovery

Reconnecting or relaunching runs bootstrap and reuses a valid grant. While connected,
main-frame HTTP 401 responses and authentication navigation trigger silent recovery;
ordinary tapped external links remain external. Foreground activation checks for a
missing/expired session cookie without a timer. HTTP 403 is a permission error, not a
reason to repeatedly sign in.

Recovery recreates the WebView in the same logical store and preserves the last
intended app page, query, and conversation. It never silently changes workspace.
A subsequent automatic attempt requires both trusted app activity after the last
attempt and a 60-second cooldown. Repeated failure returns to setup; explicit Reload
starts a new user-requested attempt.

If cookie creation rejects a cached access token with HTTP 401, bootstrap performs
one forced refresh and retries cookie creation once. Other HTTP failures do not
trigger this retry or delete a valid grant. Missing/revoked credentials during silent
recovery show a native **Sign In** choice instead of automatically opening a browser.
Accepting it preserves the return page; cancelling quietly returns to setup.

## Local sign-out

**Sign Out of Workspace** is available in the native server menu, which stays
reachable for workspace connections. Trusted workspace pages can also call
`window.omnigentNative.signOut()`. That capability is not exposed for other server
types. Same-context logout navigation (`/auth/logout`, `/logout`, or an explicit
`/login.html?logout=1`) is handled as local sign-out, not as a session-expiry loop.

Sign-out invalidates pending authentication/recovery, removes that context's
Keychain credentials, and clears its isolated WebKit data through the same serialized
mutation queue used for cookie installation. It also removes the matching default
server so relaunch does not immediately reconnect; recents remain available for an
explicit future connection. Other workspaces' credentials, defaults, and stores are
not cleared.

A non-secret pending-cleanup marker is recorded before asynchronous cleanup starts.
If cleanup is interrupted or fails, the next connection must finish it before using
credentials or starting a new login. Cleanup survives view/caller cancellation;
late cookie results remain invalid even after cleanup finishes. An error is shown if
sign-out could not finish—there is no fallback to reusing the old credentials.

This is local app sign-out. It does not revoke all provider sessions or sign the user
out of Safari/IdP SSO, and a later explicit sign-in may reuse that browser SSO.

## Verification

Run these focused suites in Xcode's Test navigator:

- `DatabricksOAuthConfigurationTests`
- `DatabricksOAuthAttemptTests`
- `DatabricksOAuthClientTests`
- `DatabricksOAuthIssuerTests`
- `DatabricksLoginManagerTests`
- `DatabricksCredentialStoreTests`
- `DatabricksTokenManagerTests`
- `DatabricksSessionClientTests`
- `DatabricksWebContextTests`
- `DatabricksWorkspaceBootstrapTests`
- `DatabricksLifecycleTests`
- `DeepLinkTests` (conversation paths preserve existing workspace queries)

They use synthetic tokens and fake browser sessions; they never log in to a live
workspace. Credential-store tests use real Keychain APIs under unique test-only
service names and delete only their own scoped items. Other tests inject an
in-memory credential store and never touch the default Keychain service. WebKit
isolation tests use uniquely named stores with synthetic cookies and clear their
own data. Network fixtures prevent requests from falling through to real services;
the redirect-transport test uses a local HTTP server without real credentials.

For a live test, use a disposable simulator/device profile and two workspaces you
are authorized to access:

1. Build with a registered public client ID; verify the processed Info.plist and
   associated-domain provisioning for the exact bundle ID.
2. Connect to a workspace URL, including `o` where required. Complete the system
   auth sheet and confirm the intended Omnigent page and API access work.
3. Relaunch and verify it reconnects without another browser prompt while the
   grant remains valid.
4. Connect to a second workspace (including a different `o` on the same host).
   Switch back and verify sessions and web storage do not mix.
5. Cancel sign-in, then retry; the original workspace URL/query should remain in
   the setup form. Open a conversation link and verify its path/context survive.
6. In the disposable context, expire/remove its session cookie, then navigate or
   foreground the app. Confirm silent recovery preserves the current conversation.
   Test revoked credentials separately: the app should ask before opening sign-in.
   Debug builds carry a floating **Debug** menu on workspace connections that
   breaks one piece of the live session on demand—session cookie, access token,
   an access token the workspace will reject, or the refresh token—and then
   states the behavior to expect. It is compiled out of release builds.
7. Sign out using the native menu, relaunch, and confirm setup remains visible.
   Explicitly reconnect and verify a fresh native sign-in occurs without old web
   state. Verify the other workspace still works.
8. Check a Databricks Apps URL and a generic OIDC server for unchanged behavior.

Do not put private hosts, IDs, tokens, cookies, callback URLs, or unredacted network
captures in issues, PRs, screenshots, or maintained examples.

References: [Databricks U2M OAuth](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-u2m),
[Apple HTTPS callbacks](<https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession/callback/https(host:path:)>),
[associated domains](https://developer.apple.com/documentation/xcode/supporting-associated-domains),
[Databricks refresh rotation](https://docs.databricks.com/aws/en/integrations/single-use-tokens),
[OAuth refresh grants](https://www.rfc-editor.org/rfc/rfc6749#section-6).
