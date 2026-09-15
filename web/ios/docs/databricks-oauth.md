# Databricks OAuth on iOS

The native OAuth layer is available for workspace-hosted Omnigent, but is **not
connected to WebView loading yet**. Workspace and Databricks Apps sign-in still
run inline; generic OIDC is unchanged. Token persistence, refresh, and platform
session-cookie bootstrap are separate integration steps.

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

The public and internal flavors have different bundle IDs; verify association
for each shipped flavor. This repository cannot authorize either app on the
Databricks-owned callback domain. Its default URL is **not proof that domain
association or OAuth registration has been provisioned**.

If you override the redirect **host**, also change the Associated Domains entry
in the target's entitlements (or supply your own `CODE_SIGN_ENTITLEMENTS` file)
and provision the new domain's AASA file. Overriding only the path on an already
associated host requires updating the OAuth registration, not the entitlement.
Changing the build setting alone does not establish a domain association.

## Native flow

`DatabricksLoginManager.signIn` takes a workspace URL, validated build
configuration, and the presenting window. It returns access/refresh tokens and
expiry **in memory**; callers must not log them or send them to JavaScript.

- Fresh cryptographic state and S256 PKCE verifier for every attempt.
- Workspace-scoped `/oidc/v1/authorize` and `/oidc/v1/token`, using the documented
  `all-apis offline_access` U2M scopes. Cookie-exchange scope requirements still
  need confirmation before activation.
- Exact callback destination and state validation before exchanging the code.
- Native, form-encoded token POST in a cookie/cache/credential-isolated session;
  no HTTP redirects, even within the same origin.
- One sign-in at a time, cancellation of browser and token exchange, and rejection
  of duplicate or stale callbacks. Provider error descriptions are not surfaced.
- Normal browser SSO rather than forced ephemeral browsing.

## Verification

Run these focused suites in Xcode's Test navigator:

- `DatabricksOAuthConfigurationTests`
- `DatabricksOAuthAttemptTests`
- `DatabricksOAuthClientTests`
- `DatabricksLoginManagerTests`

They use synthetic tokens and fake browser sessions; they never log in to a live
workspace. The redirect-transport test uses a local HTTP server without real
credentials. Also inspect Debug and Release processed Info.plists using build
overrides, and the app's signing entitlements. Live browser/AASA verification is
still required when cookie bootstrap connects this layer to workspace login.

References: [Databricks U2M OAuth](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-u2m),
[Apple HTTPS callbacks](<https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession/callback/https(host:path:)>),
[associated domains](https://developer.apple.com/documentation/xcode/supporting-associated-domains).
