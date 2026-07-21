# Design: omnigent-slack web-auth page for Databricks-App-hosted servers

Status: **implemented.** The bot runs its own **custom U2M OAuth app**
(authorization code + PKCE, `offline_access`) so each user signs in once and the
bot holds a **durable, refreshable** token. Code lives in
`omnigent_slack/databricks_oauth.py` (the OAuth client + PKCE),
`omnigent_slack/enrollment_state.py` (signed OAuth `state` + nonce),
`omnigent_slack/webauth.py` (the OAuth redirect callback), and the `databricks`
branch of `config.py` / `setup.py` / `auth_manager.py` / `app.py`.
Operator/deploy guide: [`../deploy/databricks/README.md`](../deploy/databricks/README.md).

## Goal

Let a Slack user drive an Omnigent server that is deployed as a
Databricks App in **header/proxy mode**, where:

- the Databricks Apps proxy authenticates every request and injects
  `X-Forwarded-Email` (the app trusts it verbatim; the proxy is the only
  reachable path — see the Omnigent server deploy's `deploy/databricks/README.md`),
- the Omnigent server cannot mint its own tokens (`mint_runner_token` returns `None` in
  header mode, `omnigent/server/auth.py`),
- Slack events arrive over a Socket-Mode websocket with **no** authenticated
  HTTP request, so there is no `x-forwarded-access-token` to relay and no
  unauthenticated Omnigent endpoint to run the existing device flow against.

## Core idea

Register a **custom U2M OAuth app** in the Databricks workspace and have
omnigent-slack drive its **authorization-code + PKCE** flow through a small web
page it serves as its own Databricks App. A Slack user clicking the enrollment
link is sent to the workspace `/oidc/v1/authorize` screen, signs in and
consents, and Databricks redirects back to the bot's `/auth/callback` with a
single-use code. The bot exchanges that code (PKCE-bound) at `/oidc/v1/token`
for an **access + refresh** token pair with `offline_access`, and forwards the
access token as the bearer for that user's requests to the Omnigent server (Databricks'
on-behalf-of pattern). Because the grant includes a refresh token, the bot
rotates it transparently — the user signs in once, not hourly.

## Scope / blast radius (current state: `all-apis`)

The blast radius of a stored token is bounded by the **scopes the custom OAuth
app requests** (`OMNIGENT_SLACK_DATABRICKS_SCOPES`; `openid` + `offline_access`
are forced on for identity + refresh). The mechanism *can* be scope-limited — but
**today we request `all-apis`**, which is effectively a workspace-broad user
credential, not a narrowly-scoped one. Be honest about this: a stolen token store
leaks `all-apis`-level access for each enrolled user, the same breadth a
`databricks-cli` token carries.

Why `all-apis` anyway: the token's scopes must be a **superset** of the scopes
the Omnigent server declares, or its Databricks proxy rejects the token (401
on `/api`, 302→login elsewhere). The Omnigent server accepts `all-apis`, and we have not
yet confirmed a narrower scope its proxy will accept while still authenticating.
So `all-apis` is the best option available right now — the flow works and is
per-user and refreshable — but narrowing the scope is a real, tracked follow-up
(see Weaknesses #1 and Open questions #1), not a solved problem.

Mitigations that DO hold regardless of scope: the token is stored encrypted at
rest (Fernet), never logged, and revoked on `/omnigent logout`; and it is a
per-user delegated token (the request maps to the real user via the proxy's
`X-Forwarded-Email`), not a shared service-principal credential.

## Flow

### Enrollment (once per Slack user) — as implemented

1. Slack user runs `/omnigent`; bot has no valid token for
   `(team, user, server)`.
2. Bot looks up the user's email via Slack `users.info`, generates a **PKCE
   code verifier** + a single-use **nonce**, stashes the verifier under the
   nonce in memory, and posts an **enrollment link** — the workspace authorize
   URL `https://<workspace>/oidc/v1/authorize?...&state=<HMAC-signed: team,user,
   email,team_name,nonce,issued_at>&code_challenge=<S256>`. If the email can't
   be resolved (missing `users:read.email` scope), the bot **fails closed** — no
   link is issued.
3. User clicks → browser lands on the Databricks authorize screen, signs in
   (SSO) and consents. **This screen is the consent** — the user actively
   authenticates.
4. Databricks redirects back to `GET /auth/callback?code=<single-use>&state=<…>`
   on the bot's own App URL. The GET handler:
   a. verifies the signed `state` (HMAC-SHA256, TTL-bounded);
   b. **consumes** the PKCE verifier for the state's nonce (single-use — a
      replayed redirect finds none and is refused);
   c. exchanges the code at `/oidc/v1/token` (PKCE `code_verifier`, HTTP Basic
      client auth) for an **access + refresh** pair, and reads the
      authenticated email from the `id_token` (falling back to SCIM `Me`);
   d. **identity binding:** requires the OAuth-authenticated email to equal the
      email in the state (`emails_match`, case-insensitive). Mismatch → 403.
      This closes the confused-deputy (a link for user A, signed in by victim V,
      is refused);
   e. **stashes** the tokens under a single-use confirm id and renders a
      **consent page** naming the exact identities ("You are about to connect
      your Omnigent `<server>` account `<idp-email>` with Slack user
      `<slack-email>`") with a **Confirm** button — storing **nothing** yet.
5. Confirm submits a **POST** carrying the confirm id. The POST handler looks up
   the stashed tokens and stores the pair keyed by
   `(team_id, user_id, server-host)`, encrypted at rest, **with the refresh
   token**. Only this explicit action persists a credential.
6. Success page confirms which identities were linked and how to undo
   (`/omnigent logout`).

The authorization code is single-use, so it's exchanged once on the GET and the
resulting tokens held in a short-lived in-memory stash until the confirming POST
— a credential is never persisted without the user affirming the Omnigent↔Slack
account linkage on a page that names both identities.

### Per-request (steady state)

- Bot resolves the stored token for `(team,user,server)`.
- Calls the Omnigent server with `Authorization: Bearer <access token>`.
- The Omnigent server's proxy validates it and injects **`X-Forwarded-Email` for
  the real user** → `server/auth.py` header mode maps it to the Omnigent user.
- **No Omnigent server changes.** Identity mapping is entirely the existing
  header path.
- On 401 (access-token expiry) the bot refreshes via `/oidc/v1/token` using the
  stored refresh token and retries — transparently, no re-enrollment. Only if
  the refresh itself fails (grant revoked/expired) is the token dropped and the
  user prompted to sign in again.

### Transport

Identity and transport are independent. The bot talks to the Omnigent server over
ordinary HTTP request/response + SSE (`omnigent.py`), which is what it already
uses for every other server — no `wss://` tunnel through the proxy is required.
This design is only about **identity**.

## Why this beats the alternatives

The row we ship is the last one. Note its blast radius is **`all-apis` today**
(the same breadth as the second row) — the win over the alternatives is per-user
identity + refresh, NOT a narrower token. Scope-limiting is possible in principle
but unrealized (see the section above).

| Approach | Per-user identity? | Token blast radius | Refresh? | Omnigent server change? |
|---|---|---|---|---|
| SP app-to-app (M2M) | ❌ all users collapse to one SP | n/a (wrong identity) | — | none, but unusable |
| Store raw `all-apis` user token | ✅ | ❌ `all-apis` (full workspace) | ❌ ~1h | none |
| Forwarded proxy token | ✅ | ⚠️ bounded by app scopes | ❌ ~1h, no refresh | none |
| **Custom U2M OAuth app (code + PKCE)** | ✅ | ⚠️ `all-apis` today (could narrow) | ✅ `offline_access` | **none** |

## Critique

### Strengths

- **Solves the identity problem with zero Omnigent server changes** — reuses the
  existing header-mode path; the proxy does the mapping.
- **Per-user delegated identity** — each token acts as the real Slack user (via
  the proxy's `X-Forwarded-Email`), not a shared service principal. (Its blast
  radius is `all-apis` today, though — scope-limiting is a follow-up, not a
  current strength; see "Scope / blast radius".)
- **Durable via `offline_access`** — the refresh token lets the bot rotate the
  access token silently, so a user signs in once rather than hourly.
- **Never reintroduces a spoofable header** — identity always rides a real
  OAuth-validated token; the proxy's header-stripping boundary stays intact.
- **PKCE + nonce + email binding** — the code is PKCE-bound and single-use, the
  state nonce makes the callback single-use, and the OAuth-authenticated email
  must match the signed Slack email (closes the confused-deputy).
- **Confirm-before-store** — the GET exchanges the code but stores nothing; the
  token is persisted only on the Confirm POST, so a credential is never saved
  without the user affirming the exact Omnigent↔Slack linkage on a named page.

### Weaknesses / risks (current state)

1. **Stored token is `all-apis`-broad today.** We request `all-apis`
   (`OMNIGENT_SLACK_DATABRICKS_SCOPES`; `openid` + `offline_access` always added),
   so a stolen store yields workspace-broad, per-user access — not the
   narrowly-scoped grant the mechanism theoretically allows. It stays `all-apis`
   because the token's scopes must be a **superset** of what the Omnigent server
   declares (or its proxy rejects the token), and we haven't confirmed a narrower
   scope its proxy accepts. **Follow-up:** find the Omnigent server's minimal accepted
   scope and set it here; until then this is the accepted residual risk.
2. **omnigent-slack holds refreshable user tokens for many users** — a
   higher-value target than a short-lived-token store. Mitigations in place:
   Fernet encryption at rest, in-memory-only fallback when no key is set, and no
   token ever logged. `logout` best-effort revokes the refresh token at
   `/oidc/v1/revoke` before deleting the local copy, so the grant is actually cut
   off (when the OAuth app exposes revocation). Still wants: KMS-backed key (not
   an env var) and audit logging.
3. **Single app replica required.** Two reasons: (a) the bot is a Slack
   Socket-Mode consumer, so a second replica opens a second socket and
   double-processes every event (duplicate turns); (b) the PKCE verifier /
   unconfirmed-token stashes awaiting each callback are held in process memory
   keyed by the state nonce, so a callback routed to another replica fails closed
   ("link expired"). Databricks Apps are single-instance by default and this must
   stay off — do NOT enable Horizontal scaling (Beta). There's no Asset Bundle
   field to pin instance count during the Beta (it's UI-only), so it's enforced by
   not enabling it + the warnings in `deploy/databricks/README.md` and
   `databricks.yml`. A shared/persistent nonce store would remove reason (b) but
   not (a).
4. **Consumer vs workspace access** — a Slack user with only consumer access may
   authenticate at the enrollment page but still be rejected by the Omnigent server
   (mirrors the CLI "not assigned to this application" case).

### Security properties

- **Confused-deputy is closed by three layers.** (1) The enrollment `state`
  carries the Slack user's email (from `users.info`), and the callback requires
  the OAuth-authenticated email (from the `id_token`, falling back to SCIM `Me`)
  to match it — a link issued for user A, signed in by victim V, is refused
  (403). (2) The authorization code is PKCE-bound and single-use at Databricks.
  (3) The state's nonce keys a single-use, in-memory PKCE verifier, so a replayed
  redirect finds no verifier and is refused.
- **`state` is unforgeable and single-use.** HMAC-SHA256 (keyed by a dedicated
  `OMNIGENT_SLACK_DATABRICKS_STATE_SECRET`, separate from the OAuth client secret
  and required to be ≥ 32 chars so it isn't offline-brute-forceable from a
  captured state), TTL-bounded, with a per-enrollment nonce.
- **id_token trusted on the strength of TLS.** The `email` claim is read without
  JWKS signature verification because the token arrives directly over TLS from
  the workspace token endpoint. HTTPS is therefore **enforced** for every URL the
  flow depends on — the workspace host, `OMNIGENT_SERVER_URL` (carries the bearer),
  and the web-auth base URL / OAuth redirect target (carries the `?code=` and the
  consent page's PII) — each rejecting non-loopback `http://` in
  `_check_databricks_config` / the `server_url` validator (loopback excepted for
  local dev). If the id_token trust ever needs to relax, add real JWKS id_token
  verification first.

## Open questions / follow-ups

1. **Minimum scopes** the Omnigent server's proxy will accept (must be a superset of
   the app's declared scopes) — narrow `OMNIGENT_SLACK_DATABRICKS_SCOPES` from
   the `all-apis` default to the Omnigent server's exact scope once confirmed.
2. **Pending-verifier durability:** the in-memory PKCE verifier map means a bot
   restart between link-issue and callback loses it (the user re-runs
   `/omnigent`) and the app must run as a single replica (weakness #3). A
   shared/persistent store keyed by nonce would remove both edges.
3. **Token-store hardening:** still wants a KMS-backed encryption key (not an env
   var) and audit logging (weakness #2).
