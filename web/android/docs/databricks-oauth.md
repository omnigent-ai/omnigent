# Databricks OAuth on Android

The Android app uses a public-client Databricks OAuth flow for workspace-hosted
Omnigent. The protocol core is intentionally separate from WebView activation so
the browser, credential, and session-bootstrap layers can be reviewed before a
workspace stops using its existing inline login.

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
have no credentials, query, or fragment. Android App Link activation also
requires the callback host to publish an `assetlinks.json` statement for the
shipped application ID and signing certificate, and the exact redirect must be
registered on the Databricks OAuth client. Repository configuration alone
cannot provision either external dependency.

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
caching disabled. They do not use Android WebView's cookie APIs. Callers must
run network operations off the main thread and must never log or send tokens to
JavaScript.

## Workspace identity

A credential scope is the normalized HTTPS workspace origin, optional `o`, and
exact client ID. Host casing, paths, fragments, and explicit port 443 do not
create a second scope. Different `o` values on a shared host remain separate.
Databricks Apps, unrelated domains, duplicate `o` values, non-decimal IDs, and
lookalike hosts are rejected.

This file will be expanded by the credential, WebView-profile, recovery, and
sign-out layers as they are activated.
