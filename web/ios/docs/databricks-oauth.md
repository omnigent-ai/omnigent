# Databricks OAuth on iOS

The native OAuth layer is available for workspace-hosted Omnigent, but is **not
connected to WebView loading yet**. Workspace and Databricks Apps sign-in still
run inline; generic OIDC is unchanged. Native token persistence and on-demand
refresh, workspace query context, and issuer discovery are implemented. Platform
session-cookie bootstrap, isolated per-workspace WebKit data stores, and WebView
activation remain separate integration steps.

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
  the newer state. There are no timers, background polling, or automatic HTTP retries.
- `clear(for:)` removes native credentials only. Cookie cleanup, provider logout,
  and suppression of automatic re-login belong to the later WebView lifecycle work.

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

## Verification

Run these focused suites in Xcode's Test navigator:

- `DatabricksOAuthConfigurationTests`
- `DatabricksOAuthAttemptTests`
- `DatabricksOAuthClientTests`
- `DatabricksOAuthIssuerTests`
- `DatabricksLoginManagerTests`
- `DatabricksCredentialStoreTests`
- `DatabricksTokenManagerTests`
- `DeepLinkTests` (conversation paths preserve existing workspace queries)

They use synthetic tokens and fake browser sessions; they never log in to a live
workspace. Credential-store tests use real Keychain APIs under unique test-only
service names and delete only their own scoped items. Other tests inject an
in-memory store and never touch the default Keychain service. The redirect-transport
test uses a local HTTP server without real credentials. Also inspect Debug and Release processed Info.plists using build
overrides, and the app's signing entitlements. Live browser/AASA verification is
still required when cookie bootstrap connects this layer to workspace login.

References: [Databricks U2M OAuth](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-u2m),
[Apple HTTPS callbacks](<https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession/callback/https(host:path:)>),
[associated domains](https://developer.apple.com/documentation/xcode/supporting-associated-domains),
[Databricks refresh rotation](https://docs.databricks.com/aws/en/integrations/single-use-tokens),
[OAuth refresh grants](https://www.rfc-editor.org/rfc/rfc6749#section-6).
