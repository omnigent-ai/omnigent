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
- `workspace-selector-validation`: a URL's `?o=` routes the current command
  without replacing saved login details. Login and launch auth probes compare
  it with the server's workspace header, even on HTTP 200; a mismatch names both
  workspaces and asks for the full URL. Only successful verification saves the
  selector. Servers without the header retain their existing login behavior.
- `cli-browser-ticket-consent`: an OIDC sign-in opens a ticket-specific browser
  login and then an **Authorize sign-in** page. Compare its code with the CLI,
  choose **Approve** to complete sign-in, or **Deny** to stop it. Signing in
  without approving leaves the CLI waiting. Older clients open an upgrade page
  and cannot start a new session until updated.
- `databricks-credential-kind`: the host authenticates with a user OAuth login
  made by the Databricks CLI, a personal access token, or a machine-to-machine
  service principal. Each is its own state to reproduce.
- `profile-selection`: when several Databricks profiles match the workspace, the
  person's own profile wins over a service principal.
- `expired-credential`: an expired or revoked Databricks credential leads to a
  re-authentication prompt. When a newly launched runner explicitly reports a
  pending host sign-in, its first message waits on that same runner until the
  sign-in finishes or its bounded wait expires. A runner the server refuses on
  credentials before it connects is reported with a sign-in-again instruction
  naming `omnigent login`, not as a runner crash.
- `proxy-env`: login detects the right mode even with HTTP proxy variables set.
- `host-lifecycle`: register in the foreground or background, run as a per-user
  system service, and check status, stop, stop a session, or reset the host ID.
  A machine runs one host per host ID and Databricks workspace, even through
  two URLs for it, once the workspace ID is known from the server or a login
  record; otherwise (a non-Databricks server, or an unreachable server with no
  login record) daemons stay per URL. A second background daemon yields and
  `host --background` or `start` reports the running one, a foreground `host`
  refuses after retrying for about 6 s, and a service host waits until the
  other daemon stops. If the server reports the running daemon's host offline,
  a launch doesn't reuse it; it refuses and names the login and `host stop`
  commands that fix it. Without file locks (Windows), daemons check only each
  other's records, so two that start at the same moment can both connect.
- `host-inline-login`: `omnigent host` against a Databricks-fronted server that
  the user is not signed in to runs the login flow first; `--non-interactive`
  fails with the login command instead. States: missing Databricks SDK extra,
  missing Databricks CLI, first browser login followed by host registration,
  and a saved login reused on the next invocation.
- `desktop-ssh-connect`: in the desktop app (macOS and Linux), **Connect a host**
  connects a machine the user reaches over SSH. The app runs the system `ssh`
  with the user's config, runs `omnigent login` there with the sign-in callback
  port forwarded to the laptop, then starts `omnigent host --background`.
  States: consent, trusting a new host key, a key passphrase or password,
  installing omnigent when it is missing, finishing in the browser (with a
  confirmation code on OIDC servers), connected (the new host selected),
  already connected, connected but the new host unconfirmed (the dialog stays
  open to pick it from the host list), closing the window mid sign-in (nothing
  left running on the machine), a sign-in link the machine shouldn't print
  (refused, no browser), a window connected to a server on this computer
  (`localhost`; refused in the dialog before connecting), and failures, shown
  only in the app's own window.
  Hidden where **Run on Arca** is offered (the Databricks-internal flag on a
  Databricks-managed server).
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
- `ios-oidc-browser-sign-in`: the iOS app signs in to an OIDC server in Apple's
  sign-in sheet and never shows the identity provider inside its web view.
  States: Connect with no session (sheet), Connect with a still-valid session
  (no sheet), cancel mid sign-in, a refused sign-in (disallowed domain; relaunch
  then shows the connect screen, prefilled), relaunch or a deep link (silent, or the sheet directly when nothing can renew), session
  expiry while open (silent), an expired or revoked sign-in ("Sign in again?"
  with the reason), and sign-out. Accounts, header, older OIDC servers (ticket
  sign-in), and Databricks hosts keep their previous sign-in.
- `android-oidc-browser-sign-in`: the Android app signs in to an OIDC server in
  Auth Tab and never shows the identity provider inside its WebView. States:
  Connect with no session (Auth Tab), Connect with a still-valid session (no
  browser), cancel mid sign-in, a refused sign-in (disallowed domain), relaunch
  (silent, or "Sign in to <host> to continue." when nothing can renew), Android
  stopping the app while the browser is open, session expiry while open
  (silent), an expired or revoked sign-in ("Sign in again" with the reason), and
  sign-out. Accounts, header, older OIDC servers (ticket sign-in), and
  Databricks hosts keep their previous sign-in.

## How to get to it (user POV)

**CLI sign-in:** `omnigent login <server-url>`.

**Host:** `omnigent host <server-url>` (or `--server`), `omnigent host ""` for a
local server, `--background`, `--no-open`, `--open-in-desktop` (or
`OMNIGENT_HOST_OPEN_IN_DESKTOP=1`), `--non-interactive`, and the
management commands `status`, `stop`, `stop-session`, `reset-id`, `enable`,
and `disable`. With `--open-in-desktop`, a supported desktop app accepts
the request before navigating or asking for consent. If it cannot accept within
five seconds, the browser opens instead. URLs with base paths or selectors keep
their original destination in the browser. `--no-open` suppresses both.

**Web:** a harness that needs sign-in is marked in the harness picker, and a
session whose host went offline offers reconnect (see [sessions](./sessions.md)).

**Desktop:** Connect on the setup screen (or the server selector) to an OIDC
server; relaunching the app, New Window, deep links, and switching servers on a
signed-in server; the app's sign-out in Settings, **Server → Sign Out of Server**
in the native menu, and **Sign out of <server>** in the sidebar server picker (Databricks and OIDC servers; the next Connect opens
the browser, which is how a user switches accounts). To connect a machine over SSH,
use the SSH field under the `omni host` command (desktop app only; not yet on
Windows): **Connect new host** in a new session's host menu, or the clone dialog
(from the header or the reconnect dialog's Clone tab) when no host is online or
under **Connect another host from your terminal**. Each selects the new host.

**iOS:** Connect on the connect screen to an OIDC server; a server picked in the
native server menu or the sidebar server picker; relaunching the app, deep links,
and notification taps on a signed-in server; **Sign In** in the "Sign in again?"
prompt; and sign-out from the app's settings, **Sign Out** in the native server
menu, and **Sign out of <server>** in the sidebar server picker.

**Android:** Connect on the connect screen to an OIDC server; a server picked in
the native server menu or the sidebar server picker; relaunching the app and
notification taps on a signed-in server; **Sign In** on the "Sign in to <host>"
prompt; and sign-out from the app's settings, **Sign Out** in the native server
menu, and **Sign out of <server>** in the sidebar server picker.

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
- **`cli-browser-ticket-consent`:** run
  `tests/e2e_ui/auth/test_oidc_login_flow.py::test_oidc_cli_ticket_completes_through_browser`
  against its isolated OIDC test server. Start `omnigent login <server-url>`
  with throwaway configuration, sign in in the browser, compare the code, and
  approve to complete the waiting CLI. Start a separate login and deny it to
  confirm polling stops; closing the page without deciding must leave it pending.
  Older clients should see upgrade instructions and stop polling.
- **`proxy-env`:**
  `tests/e2e/test_login_accounts_proxy_env.py::test_login_detects_accounts_auth_despite_proxy_env`
  (starts its own accounts server; run with plain `uv run pytest`).
- **`profile-selection`:**
  `tests/e2e/test_host_auth_prefers_user_over_m2m_sp_e2e.py::test_resolve_auth_for_host_must_not_select_m2m_sp_over_user_profile`
- **`workspace-selector-validation`:**
  `tests/e2e/test_host_inline_login.py::test_login_rejects_truncated_workspace_selector`
  runs the real CLI and local server behind the existing mock HTTPS workspace,
  using a test-owned PAT and CA. The proxy accepts the bearer but reports a
  different workspace. Login must fail without changing auth/config files; a
  corrected URL must succeed. This tests CLI, HTTP and file boundaries, not a
  live Databricks deployment. The login and backend unit suites cover cached,
  refreshed and header-auth responses, omitted headers, and process-local routing.
- **`expired-credential`:**
  `tests/e2e/test_expired_databricks_credential_launch.py::test_expired_databricks_credential_offers_reauth_not_edge_403`
- **Pending host sign-in:**
  `tests/server/integration/test_session_host_launch.py::test_host_session_message_waits_for_bound_runner_before_relaunch`
  checks that the original message is forwarded once after the sign-in wait,
  without replacing the runner. The `sign_in_pending` parameter covers the
  explicit pending state; ordinary startup retains its shorter grace.
  `tests/server/routes/test_runner_connect_wait.py::test_explicit_sign_in_wait_is_bounded_and_owned`
  covers cancellation, stop, replacement, process exit and expiry. The isolated
  header-mode environment has no interactive credential source, so it cannot
  drive an actual browser reauthorization.
- **Runner refused on credentials:**
  `tests/host/test_connect.py::test_watch_runner_reports_refused_credentials_as_sign_in`
  (plain `uv run pytest`) checks the sign-in instruction before a connection
  and an ordinary runner fault after one.
- **`desktop-ssh-connect`:**
  `tests/e2e/test_desktop_ssh_connect.py::test_ssh_connect_signs_in_through_forward_and_starts_host`
  runs the app's production connect code against a real `sshd`, with the laptop
  side in an unprivileged network namespace, through the real omnigent and
  Databricks CLIs and the mock workspace. It needs Linux with `unshare -rn`,
  `sshd`, node, openssl and the Databricks CLI; CI runners lack `sshd` and the
  Databricks CLI, so it skips there. The window's states render from
  `web/electron/ssh-connect/index.html` with a stub `window.sshConnect` bridge.
  The SSH field needs the desktop app (`just electron-dev`) on a server that
  serves this web build. A real Databricks sign-in needs a real remote machine
  and workspace.
- **`databricks-credential-kind`:** needs a real workspace and one test
  profile of each kind. No e2e covers the host daemon against a real workspace.
- **`host-lifecycle`:** run `omnigent host --help` and the management commands
  inside cli-setup-verify's sandbox; the system service commands change the
  user's login items, so only run them on a disposable machine. One host per
  workspace: `tests/host/test_daemon_lifecycle.py` (the `test_daemon_yields_*`
  cases, `test_daemon_without_flock_yields_to_current_workspace_record`, and
  `test_background_daemon_loser_exits_before_connecting`),
  `tests/host/test_cli_host.py`
  (`test_background_commands_report_reuse_when_the_spawned_daemon_yields` and
  the `test_foreground_host_*_workspace*` cases; offline daemons:
  `test_same_workspace_daemon_offline_reason*` and
  `test_host_background_reuses_same_workspace_daemon_only_when_online`), and
  `tests/host/test_service.py::test_service_entry_waits_while_another_daemon_serves_the_workspace`.
  A real check needs two URLs that reach one workspace, so run it only in a
  private-network sandbox against a disposable server.
- **Background harness credentials (own environment, installed Codex/Claude SDK):**
  `tests/e2e/test_background_host_harness_credentials.py` compares real foreground
  and background hosts. Saved provider env references and named passthrough keep
  the configured harness ready; unreferenced keys remain filtered. Uses isolated
  homes and synthetic credentials without model requests. The Claude SDK case
  needs bubblewrap when machine-managed settings exist. This covers fresh hosts;
  existing-daemon refresh and installed OS services need separate verification.
  `tests/host/test_service.py::test_service_persists_explicit_host_credentials_privately`
  checks launchd/systemd file contents and permissions with service commands mocked.
- **`host-inline-login` (own environment, Linux):** run
  `uv run --no-sync pytest tests/e2e/test_host_inline_login.py --reruns 0 -q`.
  It starts an isolated real server and a local HTTPS mock workspace, then
  drives the real Omnigent and Databricks CLIs through a PTY. First, with the
  Databricks CLI absent from PATH, the command must exit with a two-line error
  naming the missing CLI and giving an install link and retry guidance, without
  the unrelated `omnigent stop` hint, opening a browser, or registering a host.
  Restore the CLI and retry the same command and home: Chromium completes the
  OAuth callback, the command continues to `Connected as`, and `GET /v1/hosts`
  confirms this user's host is
  online. Restart with the same home to confirm its host ID and login are reused
  without a second browser authorization. Transcripts and host API snapshots
  remain in pytest's temporary directory; use `--basetemp=/tmp/host-onboarding`
  to keep them at a known location.
  Prerequisites: Databricks CLI, OpenSSL, and this checkout's Playwright Chromium
  (`uv run --no-sync playwright install chromium`). Missing prerequisites skip
  the journey; a passing feature-map check does not run it. The regular E2E CI
  setup does not provision the Databricks CLI or Chromium for this test.
  This proves a mock workspace's OAuth and the real local host tunnel, not a
  live Databricks workspace, event network, or remote-browser callback.
- **Inline-login component variants:**
  `tests/cli/test_login_databricks.py::test_login_apps_fails_loud_without_databricks_extra`
  checks the SDK install hint, and
  `tests/cli/test_login_databricks.py::test_login_apps_fails_loud_without_databricks_cli`
  checks the missing executable in the regular unit suite without installing
  the real Databricks CLI or browser.
  `tests/cli/test_backend.py::test_host_remote_preflight_hints_headless`,
  `tests/cli/test_backend.py::test_host_non_interactive_flag_forwarded_to_preflight`, and
  `tests/cli/test_backend.py::test_host_remote_preflight_skips_when_authenticated`
  cover headless failure, explicit suppression, and an authenticated connection.
  These stub login/connection collaborators. The process journey above covers
  foreground positional `host <url> --no-open`; `host --server`, background
  host/start, system services, and desktop/mobile onboarding are separate entry
  points and are not proved by that recording.
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
- **`ios-oidc-browser-sign-in` (iOS app, own environment):** drive the iOS app
  in the simulator per `web/ios/README.md` ("OIDC sign-in", Manual verification)
  against an OIDC server whose manifest lists the app's redirect. The
  verification instance is header mode, so it can't show this. Component tests:
  `web/ios/OmnigentTests/OidcWebSessionTests.swift`, `OidcCredentialsTests.swift`,
  and `ServerManifestTests.swift`; server flow:
  `tests/server/integration/test_oidc_native_login.py`.
- **`android-oidc-browser-sign-in` (Android app, own environment):** drive the
  Android app on an emulator per `web/android/README.md` ("OIDC sign-in", Manual
  verification) against an OIDC server whose manifest lists the app's redirect.
  The verification instance is header mode, so it can't show this. Component
  tests: `OidcSessionControllerTest.kt`, `OidcCredentialsTest.kt`, and
  `ServerManifestTest.kt` in `web/android/app/src/test/java/ai/omnigent/android/`;
  server flow: `tests/server/integration/test_oidc_native_login.py`.
- **Desktop sign-out (Server menu and sidebar picker):** connect the desktop to an
  OIDC server or a Databricks workspace, choose **Server → Sign Out of Server**,
  and confirm every window on it returns to the server selector without a message
  and the next Connect opens the browser. An incomplete sign-out still shows an error.
  The sidebar picker's **Sign out of <server>** needs a
  server serving this checkout's web build. Component tests: the
  "signs a workspace out…", "Server → Sign Out of Server…", and "holds a
  connection that starts during sign-out…" cases in `web/electron/test/main.test.js`,
  and `web/src/shell/SidebarServerPicker.test.tsx`.

## Gotchas

- Background hosts capture their environment when started; restart the host after
  changing forwarded credentials. `host enable` saves explicitly forwarded values
  in plaintext in the per-user service definition, readable only by its owner
  (0600), so the service can retain them across logins.
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
- For a fresh-machine event, check `databricks --version` before running
  `omnigent host <server-url>`. Installing Omnigent's Databricks Python extra
  does not install the Databricks CLI executable. If the CLI is missing, follow
  the printed install link, verify it is on PATH in the same terminal, and retry
  the same host command. A successful browser sign-in must also return to the
  Databricks CLI's localhost callback on the machine running the command;
  signing in on another machine does not complete that callback.
- The iOS app also needs the server to list `ai.omnigent.ios:/oauth/callback`
  in its manifest; an OIDC server without it keeps the ticket sign-in in Safari.
- The Android app also needs the server to list
  `ai.omnigent.android:/oauth/callback` in its manifest; an OIDC server without
  it keeps the ticket sign-in in the browser. Browsers without Auth Tab hand the
  redirect to the app through a Custom Tab intent instead, so check both when a
  sign-in never returns to the app.
- The desktop decides OIDC from the server's manifest, so a server reached
  through a subpath proxy (manifest not at the origin root) keeps the in-window
  sign-in. Reproduce with the reporter's exact server URL.
