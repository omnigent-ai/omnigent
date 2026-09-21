# Databricks OAuth on Android

The Android app uses a public-client Databricks OAuth flow for workspace-hosted
Omnigent. It authenticates, creates the platform web session, and installs that
session into an isolated persistent WebView profile before loading the page.

Databricks Apps are a different server type and retain inline platform SSO.
Generic Omnigent servers retain the existing ticket/poll OIDC flow.

## Build configuration

Set these Gradle project properties when building the app:

| Property                     | Default                                        | Purpose                                          |
| ---------------------------- | ---------------------------------------------- | ------------------------------------------------ |
| `databricksOAuthClientId`    | Empty                                          | Registered Databricks public OAuth client ID.    |
| `databricksOAuthRedirectUrl` | `https://login.databricks.com/mobile-redirect` | Exact HTTPS callback registered for that client. |

For example:

```sh
cd web/android
./gradlew :app:assembleDebug \
  -PdatabricksOAuthClientId=your-public-client-id \
  -PdatabricksOAuthRedirectUrl=https://login.databricks.com/mobile-redirect
```

The values are public configuration, not secrets. Do not add a client secret to
the app and do not borrow a Databricks CLI client ID. An empty client ID is
accepted by Gradle so ordinary builds continue to work, but native workspace
sign-in rejects it when configuration is loaded.

The redirect must use HTTPS on the default port, contain a nonempty path, and
have no credentials, query, or fragment. The exact redirect must be registered
on the Databricks OAuth client. Auth Tab validates the configured HTTPS host and
path before returning a result; the callback host must publish the relationship
metadata required by the browser for that verification. The same relationship
lets the narrow verified-link callback activity receive the redirect when an
older browser falls back to Custom Tabs. Repository configuration alone cannot
provision those external dependencies.

## Browser handoff and callback

AndroidX Browser Auth Tab owns the authorization surface. `MainActivity`
registers its Activity Result launcher before creation, then `AuthTabIntent`
launches the authorization URL with the exact HTTPS redirect host and path. The
browser returns the callback URI directly through `AuthResult` and closes the
authentication surface. Browsers without native Auth Tab support may present a
Custom Tabs compatibility surface. The HTTPS mobile-return page can hand that
callback to `ai.omnigent.android://mobile-redirect`, which is received by the
same narrow callback activity; an exact verified HTTPS intent filter remains an
additional compatibility path. Databricks OAuth never uses a generic
`ACTION_VIEW` handoff.

The private-scheme handoff forwards `code`, `state`, optional `iss`, or `error`
unchanged. State is opaque to the transport, including base64-encoded JSON, and
must exactly match the pending attempt. Unrelated query fields are ignored. The
OAuth authorization and token requests continue to use the registered HTTPS
`redirect_uri`; the private URI is only a browser-to-app transport.

Before launching Auth Tab, the app encrypts the short-lived attempt—workspace
scope, client configuration, state, verifier, and creation time—with an app-owned
Android Keystore key. Activity Result restoration and this encrypted record let a
recreated activity validate the callback, consume the record once, exchange the
code, and save the resulting grant. Attempts expire after ten minutes.
Cancellation, verification failure, and timeout delete the pending record;
wrong-state, duplicate Auth Tab/HTTPS/private callbacks, or stale callbacks do
not consume a newer attempt. Credentials never travel through intent extras.

## Protocol boundaries

For every authorization attempt the app creates an S256 PKCE verifier and a
cryptographically random nonce. OAuth state is standard-base64 JSON containing
`{"scheme":"ai.omnigent.android","nonce":"<random>"}`; form encoding converts
base64 padding and any reserved characters for the authorization query. The
mobile-return page uses only `scheme` to choose the private callback, while the
app compares the complete returned state string so the nonce remains the CSRF
binding.

Authorization starts at the entered workspace origin's `/oidc/v1/authorize`
endpoint with `all-apis offline_access`. Only a single, ASCII-decimal `o`
workspace hint is forwarded; unrelated page query parameters are not copied
into the authorization request.

The callback parser requires:

- the exact configured HTTPS callback scheme, host, effective port, and path;
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
explicit connection may start browser sign-in immediately. Cancellation returns
to setup with the entered workspace preserved. Authentication navigation and
main-frame 401/403 currently fail back to setup; bounded in-place recovery and
local sign-out are layered on the same coordinator/profile primitives.
