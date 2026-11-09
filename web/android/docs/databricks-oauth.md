# Databricks OAuth on Android

The Android app uses a public-client Databricks OAuth flow for workspace-hosted
Omnigent. It authenticates, creates the platform web session, and installs that
session into an isolated persistent WebView profile before loading the page.

Databricks Apps are a different server type and retain inline platform SSO.
Other Omnigent servers follow [OIDC sign-in](../README.md#oidc-sign-in), which
reuses Auth Tab and the callback receiver but never reads Databricks credentials.

## Build configuration

Set these Gradle project properties when building the app:

| Property                     | Default                                 | Purpose                                                                                     |
| ---------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------- |
| `databricksOAuthClientId`    | `omnigent`                              | Registered Databricks public OAuth client ID.                                               |
| `databricksOAuthRedirectUrl` | `ai.omnigent.android://mobile-redirect` | Exact callback registered for that client; an HTTPS app-link redirect remains configurable. |

For example:

```sh
cd web/android
./gradlew :app:assembleDebug \
  -PdatabricksOAuthClientId=your-public-client-id \
  -PdatabricksOAuthRedirectUrl=ai.omnigent.android://mobile-redirect
```

The values are public configuration, not secrets. The default `omnigent` is the
public first-party client the desktop and iOS apps also use; override it only for
a different registered client. Do not add a client secret to the app and do not
borrow a Databricks CLI client ID. An empty client ID is accepted by Gradle, but
native workspace sign-in rejects it when configuration is loaded.

The default private callback must be registered on the Databricks OAuth client
as `ai.omnigent.android://mobile-redirect` (including both slashes). No Android
Digital Asset Links file is needed for this scheme. The provider must accept it
as `redirect_uri` at both the authorization and token endpoints; changing the
Android build alone cannot register it. A custom HTTPS redirect is also supported:
it must use the default HTTPS port, contain a nonempty path, and have no
credentials, query, or fragment. HTTPS Auth Tab callbacks additionally require
Android association metadata on the callback host for the app's signing
certificate, including when a browser falls back to a verified app link. Other
apps can claim the private scheme; PKCE protects an intercepted code from
redemption without the verifier, but a verified HTTPS app link offers stronger
callback ownership.

## Verify the provider registration

Register `ai.omnigent.android://mobile-redirect` as an exact redirect URI for
the public client used by the build (default `omnigent`). Then install this
build on an Android device with a browser that supports Auth Tab and connect
to a test workspace:

1. If authorization rejects the `redirect_uri`, confirm the registration and
   the workspace's OAuth policy. A successful browser return to the app proves
   only that the authorization endpoint accepted it.
2. Complete sign-in and confirm the workspace page loads. The token endpoint
   must accept the same exact `redirect_uri` and PKCE verifier before the app
   can create a session. A callback followed by a token-exchange error means
   authorization acceptance alone was insufficient.
3. Check a browser without Auth Tab if available: its Custom Tab should hand
   the private redirect to the callback activity. Repeat after killing the
   backgrounded app to exercise pending-attempt restoration.

Do not paste callback URLs, authorization codes, verifiers, or tokens into
reports. Local unit tests validate the app's requests, not provider policy.

## Browser handoff and callback

AndroidX Browser Auth Tab owns the authorization surface. `MainActivity`
registers its Activity Result launcher before creation, then `AuthTabIntent`
launches the authorization URL with the private redirect scheme (or the exact
HTTPS redirect host and path for an HTTPS override). The browser returns the
callback URI directly through `AuthResult` and closes the authentication
surface. Browsers without native Auth Tab support may present a Custom Tabs
compatibility surface: the narrow private-scheme callback activity receives
the registered redirect instead. An HTTPS override retains the verified HTTPS
intent filter and the legacy mobile-return-page private handoff.

The default private-scheme callback carries `code`, `state`, optional `iss`,
or `error` unchanged. State is opaque to the transport, including base64-encoded
JSON, and must exactly match the pending attempt. Unrelated query fields are
ignored. Authorization and token requests use the same registered private
`redirect_uri`. For an HTTPS override they use its registered HTTPS URI; the
private handoff remains only a browser-to-app transport in that mode.

Before launching Auth Tab, the app encrypts the short-lived attempt—workspace
scope, client configuration, state, verifier, and creation time—with an app-owned
Android Keystore key. Activity Result restoration and this encrypted record let a
recreated activity validate the callback, consume the record once, exchange the
code, and save the resulting grant. Attempts expire after ten minutes.
Cancellation, verification failure, and timeout delete the pending record;
wrong-state, duplicate Auth Tab/private/HTTPS callbacks, or stale callbacks do
not consume a newer attempt. Credentials never travel through intent extras.

## Protocol boundaries

For every authorization attempt the app creates an S256 PKCE verifier and a
cryptographically random nonce. OAuth state is standard-base64 JSON containing
`{"scheme":"ai.omnigent.android","nonce":"<random>"}`; form encoding converts
base64 padding and any reserved characters for the authorization query. The
mobile-return page can use `scheme` for a legacy HTTPS redirect handoff. The
app compares the complete returned state string so the nonce remains the CSRF
binding for either redirect.

Authorization starts at the entered workspace origin's `/oidc/v1/authorize`
endpoint with `all-apis offline_access`. Only a single, ASCII-decimal `o`
workspace hint is forwarded; unrelated page query parameters are not copied
into the authorization request.

The callback parser requires:

- the exact private callback scheme and host, or the configured HTTPS callback
  scheme, host, effective port, and path for an HTTPS override;
- exactly one matching state value;
- exactly one nonempty code, or one sanitized provider error;
- at most one supported issuer.

Provider error descriptions are never surfaced. A missing issuer falls back to
the entered workspace's `/oidc` authority. A supplied issuer must be an HTTPS
Databricks workspace `/oidc` authority or an account
`/oidc/accounts/<account-id>` authority with no userinfo, custom port, query, or
fragment.

Before exchanging a code, the client fetches
`<issuer>/.well-known/openid-configuration` and requires exact `issuer` and
`token_endpoint` values. Code and refresh requests are form encoded, use no
client secret, and do not follow redirects. Token values are treated as opaque;
issuer metadata is retained so account-issued grants refresh against the same
verified authority.

OAuth requests use a native `HttpURLConnection` transport with redirects and
caching disabled. They do not use Android WebView's cookie APIs. Auth Tab result
exchange and the token coordinator run them off the main thread. Tokens must
never be logged or sent to JavaScript.

## Workspace identity

A credential scope is the normalized HTTPS workspace origin, optional `o`, and
exact client ID. Host casing, paths, fragments, and explicit port 443 do not
create a second scope. Different `o` values on a shared host remain separate.
Databricks Apps, unrelated domains, duplicate `o` values, non-decimal IDs, and
lookalike hosts are rejected.

## Credentials and refresh

Versioned token records are AES-GCM encrypted in app-private preferences. The
non-exportable AES key lives in Android Keystore and is marked usable only while
the device is unlocked. Ciphertext is bound to its logical scope through GCM
associated data, preferences keys are scope hashes, and Android backup remains
disabled in the manifest. There is no plaintext preferences or file fallback.

A process-wide token manager coordinates each scope:

- grants with more than 60 seconds remaining are reused;
- callers share one refresh operation per scope;
- cancelling one caller does not cancel an issued rotation;
- a replacement refresh token is persisted before success is returned;
- only HTTP 400 `invalid_grant` clears the scope;
- transient/network/malformed responses retain the prior record;
- clear or replacement fences late refresh results;
- a failed rotated-token write is retained in memory and retried before the old,
  consumed refresh token can be loaded again.

As on iOS, an app process loss between remote refresh rotation and local durable
write can still require a fresh sign-in because those operations cannot be
atomic.

## Session bootstrap and WebView isolation

A workspace connection does not load its URL first and infer authentication from
redirects. The native coordinator obtains a current grant, then issues
`GET /auth/session/create` with the bearer on the first request only. Redirects
are followed manually for at most eight hops and must remain on supported HTTPS
Databricks workspace domains without changing an explicit workspace ID.

The operation maintains its own cookie jar. It validates cookie domain/path scope
and requires the final page to have a nonempty `DBAUTH` cookie marked Secure and
HttpOnly. Login-page landings, unsafe redirects, missing cookies, and unexpected
HTTP responses fail before a WebView loads. A cached access token rejected with
HTTP 401 is refreshed and retried once; other failures do not trigger that retry.
The intended Omnigent path, query, fragment, and workspace hint survive the
exchange.

Each credential scope maps to a stable AndroidX WebKit profile name. The WebView
is bound to that profile before its first use. Cookie installation is serialized
per profile, clears stale profile cookies, writes the validated session cookies,
flushes them, and reads `DBAUTH` back before navigation. Other server types keep
the default profile. If the installed Android System WebView does not expose the
multi-profile feature, workspace login fails with an update prompt rather than
sharing the default cookie jar.

Android's `CookieManager` cannot enumerate every stored cookie attribute after
installation. Secure/HttpOnly/domain/path properties are therefore validated on
the native `Set-Cookie` response before install; readback verifies the resulting
name/value at the final page URL.

On relaunch, a saved grant bootstraps silently. Missing credentials show an
explicit Sign In action instead of opening the browser without user input. An
explicit connection may start browser sign-in immediately. Only a Connect or
server switch made in the running process is explicit: Android restores a killed
activity with its original intent, so a restored activity counts as a relaunch.
Cancellation returns to setup with the entered workspace preserved.

## Session recovery

Authentication navigation and main-frame HTTP 401 trigger native bootstrap again
in the same profile while preserving the last accepted app route. Foregrounding
the activity checks for a missing `DBAUTH` cookie and takes the same path. HTTP
403 is a permission error and returns to setup instead of repeatedly signing in.

Recovery can silently reuse or refresh a saved grant. If the grant is missing or
revoked, the app presents an explicit Sign In action before opening the browser.
A subsequent automatic recovery requires both a successfully ready page and a
60-second cooldown; repeated failure returns to setup rather than looping.

## Local sign-out

A successfully bootstrapped workspace exposes **Sign Out of Workspace** in the
native recovery menu and `window.omnigentNative.signOut()` to the trusted main
frame. Other server types receive neither the JavaScript method nor a native
sign-out callback.

Sign-out records a non-secret pending-cleanup marker, invalidates browser login
and refresh work, clears the scoped encrypted grant, then clears that profile's
cookies and web storage. The current/default server is removed while recents are
retained for an explicit future reconnect. A later connection must finish an
interrupted cleanup before loading credentials or starting a new browser login.
Other workspace profiles are not touched.

This is local app sign-out. It does not revoke the provider grant globally or
clear system-browser/IdP SSO, so a later explicit sign-in may reuse browser SSO.

## Debug authentication faults

Debug builds add **Debug Authentication** to the native workspace menu. It can:

- clear only the `DBAUTH` session cookie, exercising foreground/navigation recovery;
- expire the saved access token, exercising silent refresh;
- install an unexpired access token the workspace will reject, exercising one 401 refresh/retry;
- clear the saved OAuth grant, exercising the explicit Sign In prompt.

Faults are scoped to the selected workspace profile and credential record. The
menu labels and fault implementation live only in the Debug source set; Release
builds compile a no-op surface and contain none of those menu strings.
