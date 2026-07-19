# Design: omnigent-slack web-auth page for Databricks-App-hosted servers

Status: **implemented and validated on e2-dogfood.** Code lives in
`omnigent_slack/enrollment_state.py` (state signing), `omnigent_slack/webauth.py`
(enrollment web server), and the `databricks` branch of `config.py` /
`setup.py` / `auth_manager.py` / `app.py`. Operator/deploy guide:
[`../deploy/databricks/README.md`](../deploy/databricks/README.md).

> The original proposal narrowed the stored token to omnigent-dev via an
> audience-scoped token exchange. That is unavailable (Databricks rejects
> `audience` for an access-token subject), so the shipped design passes the
> forwarded token straight through, bounded by `user_api_scopes`. See
> [How least-privilege is achieved](#how-least-privilege-is-achieved).

## Goal

Let a Slack user drive an Omnigent server (`omnigent-dev`) that is deployed as a
Databricks App in **header/proxy mode**, where:

- the Databricks Apps proxy authenticates every request and injects
  `X-Forwarded-Email` (the app trusts it verbatim; the proxy is the only
  reachable path — see the server deploy's `deploy/databricks/README.md`),
- the server cannot mint its own tokens (`mint_runner_token` returns `None` in
  header mode, `omnigent/server/auth.py`),
- Slack events arrive over a Socket-Mode websocket with **no** authenticated
  HTTP request, so there is no `x-forwarded-access-token` to relay and no
  unauthenticated Omnigent endpoint to run the existing device flow against.

## Core idea

Deploy **omnigent-slack itself as a Databricks App with user authorization
enabled**, exposing a small **web auth page**. A Slack user opening that page
makes an authenticated browser request *through omnigent-slack's own proxy* —
which is exactly the authenticated HTTP context Slack traffic otherwise lacks.
The proxy forwards that user's access token (`x-forwarded-access-token`); the
bot stores it and uses it as the bearer for that user's requests to omnigent-dev
(Databricks' documented on-behalf-of pattern — pass the forwarded token
through).

## How least-privilege is achieved

The original design aimed to store a token scoped to *only* omnigent-dev via an
**audience-scoped token exchange** (`oauth2_app_client_id` as `audience`). That
turned out to be **unavailable**: Databricks rejects `audience` for an
`access_token` subject (`"audience parameter is not supported for access_token"`)
— it's only supported for PAT-type subjects (the notebook→app flow). The
`x-forwarded-access-token` is an OAuth access token, so it can't be narrowed.

Least-privilege instead comes from the app's declared **`user_api_scopes`**
(`iam.current-user:read`). The forwarded token can only do what those scopes
permit, so a stolen store leaks "act as this user within those scopes", not a
workspace skeleton key. It's not as tight as a single-app-audience token would
have been, but it is **not** a broad `all-apis` credential either. Keep
`user_api_scopes` as narrow as the omnigent-dev proxy will accept.

## Flow

### Enrollment (once per Slack user, or on token expiry) — as implemented

1. Slack user runs `/omnigent`; bot has no valid token for
   `(team, user, omnigent-dev)`.
2. Bot looks up the user's email via Slack `users.info` and posts an
   **enrollment link** to omnigent-slack's own App URL:
   `https://<slack-app-host>/auth/callback?state=<HMAC-signed: team,user,email,team_name,issued_at>`.
   If the email can't be resolved (missing `users:read.email` scope), the bot
   **fails closed** — no link is issued.
3. User clicks → browser makes a **GET** to omnigent-slack **through its own
   Databricks proxy**, which authenticates the user (SSO) and injects
   `x-forwarded-access-token` + `x-forwarded-email` for that user.
4. The `GET /auth/callback` (consent) handler:
   a. verifies the signed `state` (HMAC-SHA256, TTL-bounded);
   b. **identity binding:** requires `x-forwarded-email` to equal the email in
      the state (`emails_match`, case-insensitive). Mismatch → 403. This closes
      the confused-deputy (a link for user A opened by victim V is refused).
   c. renders a **consent page** naming the exact identities ("You are about to
      connect your Omnigent `<server>` account `<idp-email>` with Slack user
      `<slack-email>` in workspace `<name>`") with a **Confirm** button — and
      stores **nothing**.
5. Confirm submits a **POST** to the same URL. The `POST` handler re-runs the
   full validation (never trusts that a GET happened), then stores the forwarded
   `x-forwarded-access-token` **directly** (Databricks OBO — no token exchange;
   audience-scoping is unavailable for access-token subjects), keyed by
   `(team_id, user_id, omnigent-dev-host)`, encrypted at rest, empty refresh
   token.
6. Success page confirms which identities were linked and how to undo
   (`/omnigent logout`).

Splitting consent (GET) from storage (POST) means a credential is **never
persisted without an explicit user action** on a page that names the identities
— the browser user is whoever the proxy authenticated, which may not be the
person handed the link, so they must confirm *their own* identity first. It also
makes the state-mutating step a POST rather than a side-effecting GET.

### Per-request (steady state)

- Bot resolves the stored token for `(team,user,server)`.
- Calls omnigent-dev with `Authorization: Bearer <forwarded token>` (+
  `X-Databricks-Org-Id` routing, reusing `databricks_request_headers`).
- omnigent-dev's proxy validates it and injects **`X-Forwarded-Email` for the
  real user** → `server/auth.py` header mode maps it to the Omnigent user.
- **No omnigent-dev changes.** Identity mapping is entirely the existing header
  path.
- On 401 (expiry) there is no refresh token, so the stored token is dropped and
  the user re-enrolls via a fresh link (same model as `oidc` session JWTs).

### Transport

Identity and transport are independent. The bot talks to omnigent-dev over
ordinary HTTP request/response + SSE (`omnigent.py`), which is what it already
uses for every other server — no `wss://` tunnel through the proxy is required.
This design is only about **identity**.

## Why this beats the alternatives

| Approach | Per-user identity? | Token blast radius | omnigent-dev change? |
|---|---|---|---|
| SP app-to-app (M2M) | ❌ all users collapse to one SP | n/a (but wrong identity) | none, but unusable |
| Store raw `all-apis` user token | ✅ | ❌ full workspace | none |
| **Web page + forwarded token, `user_api_scopes`-limited** | ✅ | ✅ limited to `user_api_scopes` | **none** |

## Critique

### Strengths

- **Solves the identity problem with zero omnigent-dev changes** — reuses the
  existing header-mode path; the proxy does the mapping.
- **`user_api_scopes` bounds the stored token** — not a single-app-audience
  token (unavailable, see above), but a narrowly-scoped one, not a workspace
  skeleton key.
- **Never reintroduces a spoofable header** — identity always rides a real
  proxy-validated token; the proxy's header-stripping boundary stays intact.
- **Enrollment anchor is legitimate** — the web page manufactures the
  authenticated HTTP context Slack lacks, using a first-class Apps feature
  (user authorization), not a hack.
- **Confirm-before-store + email binding** — a credential is never persisted
  without an explicit user action on a page naming the exact identities, and the
  browser's email must match the signed Slack email (closes the confused-deputy).

### Weaknesses / risks (current state)

1. **No refresh — hourly re-enrollment.** The forwarded `x-forwarded-access-token`
   is ~1h with no refresh token, so a user must re-click the enrollment link
   about once an hour. Stored with `refresh_token=""`; on 401 the token is
   dropped and re-enrollment is prompted. Poor fit for a chat bot where a user
   returns after a gap. **Resolved-if-needed by:** omnigent-slack running its
   own auth-code+PKCE OAuth client with `offline_access` (durable, refreshable)
   — deferred; not built.
2. **Token is scoped by `user_api_scopes`, not to a single app.** Audience-scoped
   exchange (which would restrict the token to omnigent-dev alone) is
   unavailable — Databricks rejects `audience` for an access-token subject. So a
   stolen store yields tokens usable for whatever `user_api_scopes` grants
   (`iam.current-user:read` today), not a single-app credential. Keep
   `user_api_scopes` as tight as the server proxy will accept.
3. **No server-side revocation on logout.** Because it's stored with no refresh
   token, `logout`/`logout_all` only delete the local copy; the token stays
   live on Databricks until it expires (~1h). "Logout" doesn't cut off an
   already-exfiltrated token.
4. **omnigent-slack holds user tokens for many users** — a higher-value target
   than the Omnigent-delegated-token store. Mitigations in place: Fernet
   encryption at rest, in-memory-only fallback when no key is set, and no token
   ever logged. Still wants: KMS-backed key (not an env var) and audit logging.
5. **Proxy-origin trust.** Security assumes the app port is reachable only
   through the Apps proxy (which strips client-supplied `x-forwarded-*`). The
   callback fails closed on *absent* identity headers, but has no proof-of-proxy
   check against *forged* ones if the port is ever reachable off-proxy. Inherent
   to header-mode; same assumption the server itself documents.
6. **Consumer vs workspace access** — a Slack user with only consumer access may
   authenticate at the enrollment page but still be rejected by omnigent-dev
   (mirrors the CLI "not assigned to this application" case).

### Resolved (were open questions)

- **Confused-deputy / `state` binding (was the top risk):** closed by two
  layers. (1) The enrollment `state` carries the Slack user's email (from
  `users.info`), and the callback requires the proxy's `x-forwarded-email` to
  match it — a link issued for user A, opened by victim V, is refused (403).
  (2) Nothing is stored on the GET; the user must click **Confirm** on a page
  that names their exact Databricks + Slack identities, and only that POST
  persists the token. TTL-replay is benign (same-identity idempotent).
- **Token exchange:** does NOT work — `audience` is rejected for access-token
  subjects. Replaced by direct forwarded-token pass-through (Databricks OBO).
- **Per-user identity at the server:** confirmed on e2-dogfood — the forwarded
  token authenticates to omnigent-dev's proxy and yields the correct per-user
  `X-Forwarded-Email` (verified end-to-end).

## Open questions / follow-ups

1. **Refresh model:** accept hourly re-enrollment, or build the auth-code+PKCE
   `offline_access` client for a durable, revocable token? (Product/UX call.)
2. **Minimum `user_api_scopes`** the omnigent-dev proxy will accept while still
   emitting `X-Forwarded-Email` — keep it as tight as possible.
3. **Single-use `state`:** currently replay is TTL-bounded but benign
   (same-identity); a server-side consumed-marker would make it strictly
   single-use if we ever want it.
