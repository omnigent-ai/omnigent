# Delegated Auth — Device Authorization Grant (RFC 8628)

> **IMPLEMENTED.**
>
> A generic, client-agnostic delegated-login mechanism. Slack is the first
> consumer (`integrations/slack/`), but the server side carries no
> Slack-specific concepts — the requesting application names itself with the
> RFC 8628 `client_id` (a public string like `"slack"`; display/audit only).
> It is a public OAuth client by default (no client secret), with an
> **optional** shared secret (`OMNIGENT_DEVICE_CLIENT_SECRET`) that gates the
> client-facing endpoints when set — see the phishing mitigations below.
>
> Server: `omnigent/server/routes/device_auth.py` (endpoints + the
> `mint_delegated_token` / `DELEGATED_SCOPE` it owns),
> `omnigent/server/device_grant_store.py`, `SqlDeviceGrant` +
> `device_grants` migration (`d1e2f3a4b5c6`), and scope + revocation
> enforcement in `omnigent/server/auth.py` (`delegated_path_allowed`,
> `set_grant_revocation_check`). Wired in `omnigent/server/app.py`,
> **opt-in and default-off** via `OMNIGENT_DEVICE_GRANT_ENABLED` (the
> `/oauth/*` routes are unmounted unless it is truthy), and then only in
> **accounts** mode (OIDC delegates login to the IdP via the cli-ticket
> flow and never mounts these routes).
> Slack: `integrations/slack/src/omnigent_slack/oauth.py`,
> `tokens.py` (Fernet-encrypted `oauth_tokens`), `auth_manager.py`, plus
> the bearer/refresh wiring in `omnigent.py` (`ClientAuth`,
> per-`(server,user)` pool). Login is folded into the `/omnigent` setup
> modal; `/omnigent logout` revokes + clears.
>
> **Auth-mode selection (Slack).** The bot probes the server's mode
> (`oauth.probe_auth_mode` → `GET /v1/me`, mirroring the CLI) and picks
> the flow: **accounts → this device grant**; **oidc → the server's
> cli-login ticket flow** (`/auth/cli-login` + `/auth/cli-poll`), where
> the user signs in at the IdP and the bot stores the server's session
> JWT (no device grant, no refresh token). Both surface through one
> `oauth.PendingLogin` shape so the setup/auth-manager code is
> flow-agnostic. **Header/proxy mode is unsupported** — the server mints
> no token and mounts no device-grant/cli-login router in that mode
> (`app.py`: device auth is `oidc`/`accounts` only), so `start_login`
> raises a clear error rather than firing a request the server would 404.
>
> Tests: `tests/server/test_device_auth.py`, and the Slack
> `test_oauth.py` / `test_tokens.py` / `test_client_auth.py` /
> `test_auth_manager.py`.

## Problem

The Slack integration (`integrations/slack/`) is a standalone Socket-Mode
process that calls each user's Omnigent server over HTTP + SSE
(`OmnigentClient` / `OmnigentClientPool`). Today it sends **every request
unauthenticated**: the pool is *"one unauthenticated client per server URL"*
(`omnigent.py:337`), and any server with auth enabled returns 401, which the
bot converts into a dead-end *"authentication … isn't supported yet"* setup
error (`omnigent.py:23`, `setup.py:144`).

So the bot only works against auth-disabled servers, and when it does work the
server sees a single shared anonymous identity — it cannot tell one Slack user
from another, cannot scope permissions, and cannot audit who did what.

We want each Slack user's turns to reach the Omnigent server **as that user's
own authenticated identity**, without the Slack process ever handling the
user's Omnigent credentials.

## Topology and trust

```
  omnigent server   <->   slack socket server   <->   slack.com   <->   user
  (Auth + Resource        (OAuth client /             (transport)      (browser =
   Server)                 "device")                                    Resource Owner)
```

Slack relays all messages between the user and the socket server, so **no
Omnigent credential may pass through Slack**. The user authenticates directly
against the Omnigent server in their own browser, out of band. This is exactly
the shape of the **OAuth 2.0 Device Authorization Grant (RFC 8628)**: a device
that cannot host a browser obtains a code, the user approves out-of-band, and
the device polls for a token.

Role mapping:

| RFC 8628 role            | Here                                            |
|--------------------------|-------------------------------------------------|
| Authorization Server     | Omnigent server (`/oauth/device/*`, `/oauth/token`) |
| Resource Server          | Omnigent server (existing `/v1/**` APIs)        |
| Client / "device"        | Slack socket server                             |
| Resource Owner           | The Slack user, authenticating in their browser |
| Out-of-band channel      | Slack (delivers the verification link only)     |

## What already exists (reused, not rebuilt)

RFC 8628 primitives are absent (no `device_code` / `user_code` /
`verification_uri` anywhere), but the substrate is all present:

- **Poll-endpoint shape** — `POST /auth/cli-login` + `GET /auth/cli-poll` with
  202-pending / 200-done / 410-expired semantics (`routes/auth.py:484`).
- **Atomic single-use token redemption** — `SqlAlchemyAccountStore.redeem_token`
  uses `UPDATE … WHERE redeemed_at IS NULL` + rowcount so at most one caller
  wins under concurrency (`accounts_store.py:329`). The new grant store copies
  this pattern.
- **Session JWT minting** — `mint_session_token(user_id, secret, ttl, provider)`
  (`oidc.py:53`), HS256 with `sub`/`iat`/`exp`/`provider`.
- **Bearer validation** — `UnifiedAuthProvider._check_cookie` already accepts
  `Authorization: Bearer <jwt>` and validates the same claim shape
  (`auth.py:477`). Delegated access tokens validate through this path unchanged.
- **Browser consent under accounts mode** — the `accounts` provider already
  establishes the browser identity via its session cookie; the consent page
  runs behind it. (This is why the grant mounts in accounts mode only — see
  the mount restriction below.)
- **Open-redirect hardening** — `_sanitize_return_to` (`routes/auth.py:150`) is
  reused for the post-login bounce back to the consent page.

## Design decisions (agreed)

1. **Public by default, optional client secret.** The baseline boundary is
   the secret `device_code` the client holds, the ephemeral verification
   link, and authenticated in-browser consent; initiation is per-IP
   rate-limited and nothing is granted until a real user approves. On top of
   that, setting `OMNIGENT_DEVICE_CLIENT_SECRET` on the server gates the
   **client-facing** endpoints (authorize / token / revoke) behind a shared
   secret header (`X-Omnigent-Client-Secret`, constant-time compared), so
   only an authorized client can drive the flow. The **browser** endpoints
   (consent GET / approve / deny) are never gated by it — the user's browser
   doesn't hold the secret; their trust is the session cookie + Origin check.
   Unset ⇒ endpoints stay public (backward compatible).

   *History:* the secret was implemented, removed, then reintroduced as
   opt-in. It was removed when the Slack client accepted a **user-supplied**
   server URL — shipping a shared secret to an arbitrary user-typed host was a
   secret-exfiltration/SSRF path. That objection is now gone: the Slack socket
   server's target is a **fixed operator config** (`OMNIGENT_SERVER_URL`), not
   a user-supplied URL, so the secret only ever travels to the trusted server.
2. **Refresh tokens** — short-lived access tokens (≤ 1 h) plus a rotating,
   revocable refresh token, with a 30-day absolute grant lifetime. The Slack
   server refreshes silently; a stolen access token expires quickly and a grant
   can be killed centrally or ages out on its own.

## Flow

```
 1. A Slack user opens the `/omnigent` setup modal against an
       accounts-mode server; the modal detects auth is required and starts
       the device flow (there is no separate login command).

 2. Slack server ─ POST /oauth/device/authorize ─────────────▶ Omnigent
       body: { client_id }        # public app name, e.g. "slack"
    Omnigent ─────────────────────────────────────────────────▶ Slack server
       { device_code,            # secret, HELD BY SLACK SERVER ONLY
         user_code,              # short, human-readable
         verification_uri,       # e.g. https://srv/oauth/device
         verification_uri_complete,   # verification_uri?user_code=XYZ
         expires_in: 600, interval: 5 }

 3. Slack server shows the verification link in the setup modal (initiator
       only). The device_code is NOT included — it never leaves the server
       pair; only the user_code (in verification_uri_complete) does.

 4. User clicks → Omnigent consent page (verification_uri).
       Browser authenticates via the server's accounts provider.
       Page shows: "<client_id> is requesting permission to act as YOU
       (alice@example.com) on this Omnigent server.  [Approve] [Deny]"
       plus a warning to approve only a login the user personally started.

 5. User approves → the grant is bound to the authenticated identity
       (alice@…). client_id is recorded for display/audit only, never as
       an authorization key.

 6. Slack server polls ─ POST /oauth/token ──────────────────▶ Omnigent
       grant_type=urn:ietf:params:oauth:grant-type:device_code
       { device_code }
    Responses: 400 authorization_pending | 429 slow_down |
               400 expired_token | 400 access_denied |
               400 invalid_grant |
               200 { access_token, refresh_token, expires_in, token_type }

 7. Slack server stores  (team_id, slack_user_id, server_url)
       → { access_token, refresh_token }  ENCRYPTED AT REST,
       and attaches  Authorization: Bearer <access_token>  on every
       request for that user thereafter.

 8. On 401 / near-expiry: POST /oauth/token grant_type=refresh_token →
       new access + rotated refresh.  On refresh failure (revoked/expired):
       drop tokens and re-prompt login in the setup modal.
```

The Slack `(team_id, slack_user_id)` → identity mapping lives entirely on
the Slack side (step 7). The server-side grant is client-agnostic: it
knows only the RFC 8628 `client_id` and the Omnigent identity that
approved it.

## Server-side changes

### Router `omnigent/server/routes/device_auth.py`

Mounted in `app.py` only when **`OMNIGENT_DEVICE_GRANT_ENABLED` is truthy**
(opt-in, **default-off** — the `/oauth/*` routes are absent otherwise), and
then **only in `accounts` mode** (OIDC delegates login to the IdP via the
cli-ticket flow and never mounts these routes; header mode has no
server-mintable identity — see `create_device_auth_router`, which raises if
constructed for any other source). The `device_grants` table is created
unconditionally by the migration regardless of the flag; only the router
mount is gated. This router also **owns** `mint_delegated_token` and
`DELEGATED_SCOPE` (moved here from `oidc.py`, which retains only
`mint_session_token` / `mint_session_cookie`).

- `POST /oauth/device/authorize` — **public** (rate-limited). Generates a
  high-entropy `device_code` (`secrets.token_urlsafe`, stored **hashed**), a
  short `user_code`, `expires_in`, `interval`. Persists a `pending` grant
  carrying only the public `client_id`. Returns the RFC 8628 authorize
  response. Opportunistically purges expired grants (no scheduler).
- `GET /oauth/device` — the consent page (`verification_uri`). Requires a
  browser identity via the active provider; if unauthenticated, bounce through
  the provider's normal login and return here (`_sanitize_return_to`). Prefills
  `user_code` from `verification_uri_complete`.
- `POST /oauth/device/approve` / `POST /oauth/device/deny` — authenticated
  browser actions, CSRF-gated by `_require_browser_origin` (rejects a missing
  Origin). `approve` binds the grant to the authenticated `user_id` (`sub`),
  stamps `approved_at` (the absolute-lifetime clock), and flips status to
  `approved`; `deny` flips to `denied`.
- `POST /oauth/token`:
  - `grant_type=…:device_code` — look up by hashed `device_code`; return
    `authorization_pending` / `slow_down` (interval enforcement) /
    `expired_token` / `access_denied` / `invalid_grant`, or on approval mint an
    **access token** (`mint_delegated_token`, TTL ≤ 1 h) + **refresh token** and
    return them. Single-use: an atomic `approved → redeemed` transition means
    the device_code cannot be exchanged twice.
  - `grant_type=refresh_token` — validate the presented refresh token against
    the stored hash, **rotate** it (issue new, invalidate old), mint a new
    access token. Refuses rotation past the 30-day absolute lifetime
    (`expired_token`). **Reuse detection**: presenting an already-rotated
    refresh token revokes the whole grant (token-theft signal).
- `POST /oauth/revoke` — revoke a grant: null the refresh token, mark revoked
  (the `grant_id` then reads as revoked in the denylist check). Accepts a
  `refresh_token`, or falls back to the `grant_id` on the caller's own bearer
  so a client holding only its access token can still log out. Idempotent.
  Backs `/omnigent logout`.

### New store `omnigent/server/device_grant_store.py`

Modeled on `SqlAlchemyAccountStore` — workspace-scoped, secrets stored hashed,
atomic single-use redemption, `purge_expired`. New table `device_grants`:

| column               | notes                                              |
|----------------------|----------------------------------------------------|
| `id` (grant id)      | PK with `workspace_id`                             |
| `device_code_hash`   | HMAC/SHA-256 of the device_code; never store raw   |
| `user_code`          | short code shown/typed by the user                 |
| `client_id`          | RFC 8628 client id — the requesting application (e.g. `slack`); display + audit |
| `status`             | `pending` / `approved` / `denied` / `redeemed` / `revoked` |
| `user_id`            | bound Omnigent identity, set at approval           |
| `refresh_token_hash` / `prev_refresh_token_hash` | current + prior digest (rotation + reuse detection) |
| `created_at` / `expires_at` / `approved_at` / `last_polled_at` | TTL, absolute-lifetime clock, `slow_down` timing |

### Token claims and validation (`auth.py`, `device_auth.py`)

Delegated access tokens (minted by `mint_delegated_token`) keep the existing
HS256 shape (so `_check_cookie` accepts them) plus four delegated-only claims:

- `act` — provenance, RFC 8693-style: `{ "client_id": "slack" }`, naming the
  application that obtained the grant so every delegated action is attributable
  to it.
- `scope` — set to `DELEGATED_SCOPE` (`"sessions"`). The auth layer's
  fail-closed allowlist `delegated_path_allowed` restricts a token carrying
  this scope to `/health`, `/v1/agents`, `/v1/hosts`, `/v1/sessions`,
  `/v1/runners`, `/oauth/token`, `/oauth/revoke` (exact or `prefix/…`);
  everything else — including admin / user-management (`/auth/users*`, invites,
  setup) — is rejected.
- `grant_id` — checked against the revoked-grant denylist (`is_revoked`, wired
  via `set_grant_revocation_check`) on **every** request for a delegated token,
  so revoking the grant kills the token immediately. Delegated tokens carrying
  a `grant_id` skip the credential cache (they return before the cache write),
  keeping the per-request revocation check honest without making ordinary
  (non-delegated) sessions stateful. Fail-closed: an unknown `grant_id` reads
  as revoked.
- `jti` — unique token id for audit/log correlation (not a revocation key;
  revocation is grant-scoped, not per-token).

## Slack-side changes

- **`oauth.py` (new)** — device-authorize → post ephemeral link → poll token
  endpoint (respecting `interval` / `slow_down`) → store tokens. Replaces the
  `AuthRequiredError` dead-end.
- **`omnigent.py`** — attach `Authorization: Bearer` per
  `(server_url, slack_user_id)`; on 401, refresh once and retry; on refresh
  failure, surface a re-login prompt. `OmnigentClientPool` keys clients by
  `(server_url, slack_user_id)` instead of `server_url` alone.
- **`store.py`** — new `oauth_tokens` table `(team_id, user_id, server_url)` →
  access/refresh **encrypted at rest** (key from env / secret manager, never in
  the DB). `/omnigent logout` → `POST /oauth/revoke` + local delete.
- **`setup.py`** — validation uses the user's token; auth-enabled servers become
  supported rather than rejected.
- **`config.py`** — holds the local encryption key for token storage.

## Security analysis

| # | Threat | Mitigation |
|---|--------|-----------|
| 1 | `device_code` leak → token theft | Never transits Slack or the user — only `verification_uri_complete` (a `user_code`) does. Stored hashed; single-use. |
| 2 | Link misdelivery / phishing another user | Link shown to the initiator only (in their own setup modal). Consent page names the exact Omnigent identity the grant will act as and the requesting `client_id`, and warns to approve only a self-initiated login. |
| 3 | Anyone can initiate/poll (public client) | Cheap `pending` state grants nothing until an authenticated user approves. `POST /oauth/device/authorize` is rate-limited per client IP (10/60s → 429 `slow_down`); short (10 min) `device_code` expiry; `slow_down` enforced server-side on aggressive polling; expired grants purged opportunistically. |
| 4 | Slack SQLite exfiltration → mass impersonation | Tokens **encrypted at rest**; access tokens short-lived (≤ 1 h); refresh tokens revocable. Bounded, centrally killable window. |
| 5 | Compromised Slack server acts as all users (inherent to delegation) | Reduced scope (no admin), short TTL + refresh rotation, per-grant revocation, **absolute grant lifetime (30 d) enforced on refresh** so even an un-revoked grant dies, and an `act`-claim audit trail. |
| 6 | Confused deputy — user A's token used for user B | On the Slack side, token lookup is strictly keyed by acting `slack_user_id`; the thread `owner_user_id` gate drops non-owner follow-ups (`service.py`). |
| 7 | Stale/leaked delegated token can't be revoked | Per-grant `grant_id` revocation denylist (`is_revoked`, checked every request) makes delegated-token revocation immediate — closes today's stateless-JWT gap for these higher-value tokens. |
| 8 | Refresh-token theft | Rotation on every use + **reuse detection**: the just-superseded token's digest is retained in `prev_refresh_token_hash`; presenting it (a replay) is recognised and revokes the whole grant, killing the attacker's freshly-rotated token too. |
| 9 | Transport interception | Require HTTPS for `verification_uri` and all token/bearer traffic; refuse the flow over plaintext except localhost dev. |
| 10 | Open redirect on the consent login bounce | Reuse `_sanitize_return_to` (OIDC, `routes/auth.py`) / `sanitizeReturnTo` (accounts SPA). Verified both providers reject absolute / `//` targets. |
| 11 | CSRF on approve/deny if `SameSite=none` is ever enabled | `_require_browser_origin` rejects a **missing** `Origin` on approve/deny (stricter than the shared `require_trusted_origin`, which fail-opens for non-browser clients). These routes are browser-only, so the CSRF defense no longer depends on the cookie's `SameSite`. |

### Device-code phishing — accepted risk, mitigated in depth

The canonical RFC 8628 risk: a stranger initiates a flow and tricks a victim
Omnigent user into approving the verification link, binding the grant to the
*victim's* identity while the attacker (holding the `device_code`) polls for the
token.

When no client secret is configured the endpoints are **public**, so
initiation is open — the defense is layered, not a gate:

- The consent page prominently **warns** the user to approve only a login they
  personally started and to match the code shown by the application.
- The delegated scope excludes admin / user-management endpoints.
- The grant has a 30-day absolute lifetime and is revocable; a leaked/phished
  grant self-expires even if never revoked.
- Initiation is rate-limited per IP; nothing is granted until a real user
  authenticates and approves in their own browser.

Setting `OMNIGENT_DEVICE_CLIENT_SECRET` closes initiation entirely to
unauthorized callers: without the matching `X-Omnigent-Client-Secret` header,
authorize / token / revoke return `401 invalid_client` before anything is
created, so only the operator's own client (which holds the secret) can even
start a flow. This is now shippable to the Slack client because its server
target is a fixed operator config, not a user-supplied URL — the secret only
ever travels to the trusted server. The consent-page warning, short TTL, and
absolute lifetime remain the defenses when the secret is left unset.

### Deliberate deviation from the current model

Ordinary Omnigent session JWTs are stateless and unrevocable today (revocation =
cookie deletion + expiry). Delegated tokens are higher-value — one server acts
for many users — so this design makes **delegated** tokens revocable (persisted
grant + per-`grant_id` revocation check) while leaving normal sessions
stateless. This added invariant is the main thing for reviewers to scrutinize.

## Out of scope / follow-ups

- Admin UI for listing and revoking active Slack delegations.
- Multi-replica rate limiting: the authorize throttle is in-process; a
  horizontally-scaled server would want a shared store (the grant table's
  single-use/expiry semantics already bound abuse in the meantime).
- Applying the same delegated grant to other non-browser clients (the CLI could
  use it too, superseding the in-memory `_cli_tickets` store).
- Per-scope consent granularity beyond the single "session APIs, no admin" scope.
