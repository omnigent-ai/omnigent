# Design: omnigent-slack web-auth page for Databricks-App-hosted servers

Status: **implemented (pending live validation)**. Code lives in
`omnigent_slack/databricks_auth.py` (state signing + token exchange),
`omnigent_slack/webauth.py` (enrollment web server), and the `databricks`
branch of `config.py` / `setup.py` / `auth_manager.py` / `app.py`. The open
questions at the end still require a spike against a real workspace
(e2-dogfood) — they are correctness contingencies, not blockers to the code.

Companion to [`DATABRICKS_APP_AUTH_SPIKE.md`](./DATABRICKS_APP_AUTH_SPIKE.md)
(the experiment plan). Experiment workspace:
`https://e2-dogfood.staging.cloud.databricks.com/`.

## Goal

Let a Slack user drive an Omnigent server (`omnigent-dev`) that is deployed as a
Databricks App in **header/proxy mode**, where:

- the Databricks Apps proxy authenticates every request and injects
  `X-Forwarded-Email` (the app trusts it verbatim; the proxy is the only
  reachable path — `deploy/databricks/README.md:187-195`),
- the server cannot mint its own tokens (`mint_runner_token` → `None` in header
  mode, `server/auth.py:496-508`),
- Slack events arrive over a Socket-Mode websocket with **no** authenticated
  HTTP request, so there is no `x-forwarded-access-token` to relay and no
  unauthenticated Omnigent endpoint to run the existing device flow against.

## Core idea

Deploy **omnigent-slack itself as a Databricks App with user authorization
enabled**, exposing a small **web auth page**. A Slack user opening that page
makes an authenticated browser request *through omnigent-slack's own proxy* —
which is exactly the authenticated HTTP context Slack traffic otherwise lacks.
That page runs an OAuth flow, obtains a **per-user, app-audience-scoped** token
for omnigent-dev, and the bot stores it and uses it for that user's requests.

## The security keystone: audience-scoped token exchange

The premise "store tokens only if they can only talk to omnigent-dev" is
**achievable** and documented. Databricks supports OAuth token exchange scoped
to a single app via its `oauth2_app_client_id` as `audience`:

```
POST https://<ws>/oidc/v1/token
  grant_type       = urn:ietf:params:oauth:grant-type:token-exchange
  subject_token    = <user's token>
  subject_token_type = urn:databricks:params:oauth:token-type:... (PAT/access token)
  requested_token_type = urn:ietf:params:oauth:token-type:access_token
  scope            = all-apis
  audience         = <omnigent-dev app_client_id>   # w.apps.get("omnigent-dev").oauth2_app_client_id
```

Databricks states verbatim: **"The exchanged token is scoped to the specific
app, so you can't use it to call other Databricks APIs."**

This is the mechanism that makes token storage acceptable: even though the
*subject* token may be broad, the **exchanged token the bot stores/uses is
inert against clusters/jobs/SQL/secrets/UC** — it only opens omnigent-dev's
door. A bot-DB breach then leaks "can talk to Omnigent as this user," not a
workspace skeleton key — which is the property we wanted.

> Caveat requiring verification (spike): docs demonstrate token exchange
> **notebook→app** and do not explicitly document **user-OBO→app** exchange
> preserving user identity, nor the exchanged token's **lifetime/refresh**. If
> the exchanged token is short-lived with no refresh, the bot must re-exchange
> from a stored (broader) refresh token — which reintroduces holding a broad
> refresh token at rest. See Open Questions.

## Flow

### Enrollment (once per Slack user, or on token expiry)

1. Slack user runs `/omnigent ...`; bot has no valid token for
   `(team, user, omnigent-dev)`.
2. Bot posts an ephemeral Slack message with an **enrollment link** to
   omnigent-slack's own App URL:
   `https://<slack-app-host>/auth/start?state=<signed: team,user,nonce>`.
3. User clicks → browser hits omnigent-slack **through its own Databricks
   proxy**, which authenticates the user (SSO) and injects
   `x-forwarded-access-token` + `X-Forwarded-Email` for that user.
4. omnigent-slack's page:
   - **3A path:** uses the forwarded `x-forwarded-access-token` directly as the
     subject token; or
   - **3B path:** runs auth-code+PKCE against Databricks as a registered custom
     OAuth client to obtain access + **refresh** tokens (durable).
5. omnigent-slack performs the **audience-scoped token exchange** (above) to
   mint an omnigent-dev-only token for the user.
6. Bot stores, keyed by `(team_id, user_id, omnigent-dev-host)`: the app-scoped
   access token (+ refresh material if 3B), encrypted at rest. Validates the
   `state` nonce to bind the browser session back to the Slack identity.
7. Page shows "You're connected — return to Slack."

### Per-request (steady state)

- Bot resolves the stored app-scoped token for `(team,user,server)`.
- Calls omnigent-dev with `Authorization: Bearer <app-scoped token>` (+
  `X-Databricks-Org-Id` routing, reusing `databricks_request_headers`).
- omnigent-dev's proxy validates it and injects **`X-Forwarded-Email` for the
  real user** → `server/auth.py` header mode maps it to the Omnigent user.
- **No omnigent-dev changes.** Identity mapping is entirely the existing header
  path.
- On 401 (expiry), re-exchange from refresh material; if that fails, re-post the
  enrollment link.

### Transport question (orthogonal, still open)

Whether the bot's connection to omnigent-dev can be a long-lived `wss://`
tunnel through the proxy is a **separate** unknown (spike E2). If WS-through-
proxy is unsupported, the bot uses HTTP request/response + SSE for streaming,
which the current bot already does for most calls (`omnigent.py`). This design
is about **identity**, not transport; it works with either.

## Why this beats the alternatives

| Approach | Per-user identity? | Token blast radius | omnigent-dev change? |
|---|---|---|---|
| SP app-to-app (M2M) | ❌ all users collapse to one SP | n/a (but wrong identity) | none, but unusable |
| Store raw `all-apis` user token | ✅ | ❌ full workspace | none |
| **Web page + audience-scoped exchange** | ✅ | ✅ omnigent-dev only | **none** |

## Critique

### Strengths

- **Solves the identity problem with zero omnigent-dev changes** — reuses the
  existing header-mode path; the proxy does the mapping.
- **Audience-scoped exchange makes stored tokens genuinely least-privilege** —
  directly addresses the "skeleton key" objection; a breach is contained to
  Omnigent access.
- **Never reintroduces a spoofable header** — identity always rides a real
  proxy-validated token; the proxy's header-stripping boundary stays intact.
- **Enrollment anchor is legitimate** — the web page manufactures the
  authenticated HTTP context Slack lacks, using a first-class Apps feature
  (user authorization), not a hack.

### Weaknesses / risks

1. **Exchanged-token lifetime/refresh is unproven.** If app-scoped tokens are
   ~1h with no refresh, the bot must re-exchange from a stored **broad** token
   (access or refresh), so *something* broad may still sit at rest — partially
   eroding the least-privilege win. Must verify (E4/E5). Mitigation: store only
   a refresh token (not a live broad access token) and mint app-scoped tokens
   just-in-time; or accept hourly re-enrollment (drop refresh) for max safety.
2. **User-OBO→app exchange may not be documented/supported.** Docs show
   notebook→app. If user-identity exchange isn't supported, 3A/3B may not
   produce a token that both (a) is app-scoped and (b) still resolves to the
   *user* at omnigent-dev's proxy. This is the single biggest correctness
   unknown — must verify before committing.
3. **Two apps to operate + a custom OAuth integration** (3B) — more deployment
   surface, an account-level admin action to register the client, redirect-URI
   management, and a second app's lifecycle. Heavier than the current
   single-daemon bot.
4. **omnigent-slack now holds user tokens for many users** — even app-scoped,
   it's a higher-value target than today's Omnigent-delegated-token store.
   Requires KMS-backed encryption, per-user revocation on Slack deprovisioning,
   and audit logging.
5. **`state`/CSRF binding is security-critical** — the browser session must be
   cryptographically bound to the Slack `(team,user)` that requested it, or one
   user could enroll another's Slack identity. Signed, single-use, short-TTL
   `state` is mandatory.
6. **WebSocket transport still unresolved** (E2) — if the host-tunnel model is
   needed and WS-through-proxy is unsupported, that's a separate blocker this
   design doesn't address.
7. **Consumer vs workspace access** — a Slack user with only consumer access to
   the workspace may authenticate to omnigent-slack's page but still be rejected
   by omnigent-dev (mirrors the CLI "not assigned to this application" case).
   Enrollment must surface this clearly.

### Verdict

Promising and, unlike every earlier option, it has a **credible path to
least-privilege token storage** via audience-scoped exchange — which is exactly
the property you argued makes storage acceptable. It is **contingent** on two
unverified facts: (a) a user-identity-preserving token that omnigent-dev's proxy
accepts and maps to the right user, and (b) an exchanged-token lifetime/refresh
story that doesn't force a broad token to live at rest. Both are cheap to settle
on e2-dogfood before any build.

## Open questions (settle on e2-dogfood before committing)

1. Does an **audience-scoped token** for omnigent-dev, when sent to
   omnigent-dev's URL, get accepted by the proxy AND yield the **correct
   per-user `X-Forwarded-Email`** (not a machine/SP identity)?  ← correctness
   keystone
2. Can token exchange start from a **user** credential (OBO) and preserve user
   identity, or only from notebook/PAT contexts?
3. What is the **lifetime** of the exchanged app-scoped token, and can it be
   **refreshed** without holding a broad live access token at rest?
4. Does the Apps proxy support **`wss://` WebSocket upgrade** (transport)?
5. Minimum subject-token scope needed for the exchange (is `all-apis` required
   on the subject, or does a narrower scope suffice)?
