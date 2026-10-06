# Login and host authentication

Users sign the CLI in to an Omnigent server with `omnigent login`, and register
their machine as a host with `omnigent host` so sessions can run on it. The
sign-in flow depends on the server: an accounts server asks for a username and
password, an OIDC server uses a browser ticket, a header-mode server needs no
login, and a Databricks-fronted server uses the user's Databricks workspace
credentials. Databricks credentials come in several kinds, and which one a host
actually uses decides how it behaves when a token expires. Applications can
also embed Omnigent with their own authentication provider; their users should
be able to write resources without a separate Omnigent account.

## Sub-features

- `login-auto-detect`: `omnigent login <url>` detects the server's sign-in mode.
  States: accounts, OIDC, header mode, Databricks-fronted, and a workspace URL
  with an org selector.
- `databricks-credential-kind`: the host authenticates with a user OAuth login
  made by the Databricks CLI, a personal access token, or a machine-to-machine
  service principal. Each is its own state to reproduce.
- `profile-selection`: when several Databricks profiles match the workspace, the
  person's own profile wins over a service principal.
- `expired-credential`: an expired or revoked Databricks credential leads to a
  re-authentication prompt, not a raw 403 from the workspace edge.
- `proxy-env`: login detects the right mode even with HTTP proxy variables set.
- `host-lifecycle`: register in the foreground or background, run as a per-user
  system service, and check status, stop, stop a session, or reset the host ID.
- `host-inline-login`: `omnigent host` against a Databricks-fronted server that
  the user is not signed in to runs the login flow first; `--non-interactive`
  fails with the login command instead.
- `embedded-resource-write`: an identity from a custom `AuthProvider` can
  create, rename, and reorder its own projects; unauthenticated and other users
  remain restricted.
- `embedded-auth-context`: HTTP, WebSocket, and background writes use the
  app's account mode; an embedded app must not weaken a separate accounts app.
- `desktop-oidc-browser-sign-in`: the desktop app signs in to an OIDC server in
  the system browser and never shows the identity provider inside its window.
  States: Connect with no session, Connect with a still-valid session (no
  browser), cancel mid sign-in, a refused sign-in (disallowed domain), relaunch
  (silent), session expiry while open (silent), an expired or revoked sign-in
  (back to the connect screen with the reason), and sign-out. Accounts, header,
  older servers, and Databricks hosts keep their previous sign-in.

## How to get to it (user POV)

**CLI sign-in:** `omnigent login <server-url>`.

**Host:** `omnigent host <server-url>` (or `--server`), `omnigent host ""` for a
local server, `--background`, `--no-open`, `--non-interactive`, and the
management commands `status`, `stop`, `stop-session`, `reset-id`, `enable`,
and `disable`.

**Web:** a harness that needs sign-in is marked in the harness picker, and a
session whose host went offline offers reconnect (see [sessions](./sessions.md)).

**Desktop:** Connect on the setup screen (or the server selector) to an OIDC
server; relaunching the app, New Window, deep links, and switching servers on a
signed-in server; the app's sign-out in Settings.

**Embedded API:** construct `create_app(auth_provider=...)` with the embedding
application's provider and stores, then use its authenticated REST client to
create a project, rename it, reorder projects, and read the result back.
This entry point has no CLI login step.

## Driving it with the repro environment

Preconditions: the verification instance runs a header-mode server with no
sign-in and no host daemon, so most of this feature cannot be driven there.
Never run these commands against the real `~/.omnigent` or `~/.databrickscfg`.

- **Isolated CLI loop:** use [cli-setup-verify](../.claude/skills/cli-setup-verify/SKILL.md),
  which drives the real `omnigent` binary in a PTY with a throwaway config and
  data directory, for sign-in prompts and host commands.
- **`proxy-env`:**
  `tests/e2e/test_login_accounts_proxy_env.py::test_login_detects_accounts_auth_despite_proxy_env`
  (starts its own accounts server; run with plain `uv run pytest`).
- **`profile-selection`:**
  `tests/e2e/test_host_auth_prefers_user_over_m2m_sp_e2e.py::test_resolve_auth_for_host_must_not_select_m2m_sp_over_user_profile`
- **`expired-credential`:**
  `tests/e2e/test_expired_databricks_credential_launch.py::test_expired_databricks_credential_offers_reauth_not_edge_403`
- **`databricks-credential-kind`:** needs a real workspace and one test
  profile of each kind. No e2e covers the host daemon against a real workspace.
- **`host-lifecycle`:** run `omnigent host --help` and the management commands
  inside cli-setup-verify's sandbox; the system service commands change the
  user's login items, so only run them on a disposable machine.
- **`embedded-resource-write` (in-process app and real stores):**
  `tests/server/test_external_account_authority.py::test_custom_auth_project_routes_without_users`.
  Run with plain `uv run pytest`; it constructs `create_app` with a custom
  provider and no local users table. It checks project writes and readback,
  missing identity (401), and another user's denied writes.
- **`embedded-auth-context` (middleware and store integration):**
  `tests/server/test_external_account_authority.py::test_external_auth_lifespan_and_websocket_writes`,
  `tests/server/test_external_account_authority.py::test_account_mode_is_isolated_between_apps`.
  These use small ASGI apps, not the full server lifecycle: they cover custom,
  header, OIDC, and no-provider contexts, plus concurrent app isolation.
  Run the existing `tests/db/test_account_authority.py` and
  `tests/server/test_account_revocation.py` alongside changes to shared account
  handling; revocation and saved-identity restrictions must still hold. Report backend
  skips separately. These are not browser or live identity-provider checks.

- **`desktop-oidc-browser-sign-in` (desktop shell, own environment):** drive the
  desktop app per `web/electron/README.md` ("OIDC sign-in through the system
  browser", Manual verification) against an OIDC server. The verification
  instance is header mode, so it can't show this. Component tests:
  `web/electron/test/oidc-auth.test.js`, `oidc-credentials.test.js`, and the
  "OIDC system-browser sign-in wiring" suite in `main.test.js`; server flow:
  `tests/server/integration/test_oidc_native_login.py`.

## Gotchas

- **Find out which credential kind the reporter's host uses before trusting a
  root cause.** A user OAuth login refreshes itself and a personal access
  token does not, so a story about a "stale pinned token" can be true for one
  kind and impossible for the other. Check the reporter's host sign-in record
  and profile, and reproduce with that same kind.
- A machine-to-machine service principal always authenticates, so a check that
  "the host signed in" passes even when the wrong identity was chosen. Confirm
  the host appears for the person in the web UI.
- Proxy variables in the environment can change which sign-in mode is detected.
  Reproduce with the reporter's proxy settings, including `NO_PROXY`.
- Header-mode servers, including the verification instance, never show a
  sign-in prompt. A missing prompt there proves nothing about other modes.
- An embedded app's explicitly supplied provider decides its account mode;
  the environment's default provider may differ. Test a real resource write
  through `create_app`, not only identity extraction or a standalone store.
- External authentication does not grant access to another user's resources.
  Keep the ownership checks in the journey. Project coverage alone does not
  establish every resource endpoint or a real host-registration flow.
- Sign-in can open a browser even with `--no-open`; use `--non-interactive` in
  scripted runs.
- The desktop decides OIDC from the server's manifest, so a server reached
  through a subpath proxy (manifest not at the origin root) keeps the in-window
  sign-in. Reproduce with the reporter's exact server URL.
