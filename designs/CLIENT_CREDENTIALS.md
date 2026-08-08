# Machine Auth — Client Credentials Grant (RFC 6749 §4.4)

> **IMPLEMENTED.**
>
> A single confidential client exchanges its own credentials for a delegated,
> path-scoped access token acting as a fixed machine principal. No browser, no
> per-user consent, no polling — the client *is* the resource owner.
>
> Server: `omnigent/server/routes/client_credentials.py` (the `/oauth/token`
> handler, the `MachineClientConfig` env registry, and the principal guards).
> It reuses `mint_delegated_token` / `DELEGATED_SCOPE` from
> `omnigent/server/routes/device_auth.py`, the scope enforcement in
> `omnigent/server/auth.py` (`delegated_path_allowed`), and the response
> helpers + sliding-window throttle both grants now share in
> `omnigent/server/routes/_oauth.py`. Wired in
> `omnigent/server/app.py`, **opt-in and default-off**: the router is built
> only when a machine client is configured, so `POST /oauth/token` stays
> unrouted in every deployment that configures none. Then only in the
> cookie-based auth modes (`oidc` and `accounts`), and only when the device
> grant is not mounted — that grant already owns `POST /oauth/token`.
>
> Tests: `tests/server/test_client_credentials.py`,
> `tests/server/integration/test_client_credentials_e2e.py`, and
> `tests/server/test_oauth_shared.py` for the helpers both grants share.

## Problem

The device grant (`designs/DEVICE_AUTH.md`) answers "a browserless client must
act **as a human**, without ever holding that human's credentials". It does not
answer the other case: a headless process that acts **as itself** — a scheduled
job, a webhook receiver, an internal service calling the sessions API. There is
no human to consent, so a flow built around out-of-band browser consent has
nothing to ask.

Before this grant, such a process had two bad options: drive the interactive
login of a service account (a browser in a job), or be handed the server's
cookie-signing key and mint its own session tokens (unbounded privilege, no
audit trail, no way to distinguish it from a human session).

## Role mapping

| RFC 6749 role        | Here                                            |
|----------------------|-------------------------------------------------|
| Authorization Server | Omnigent server (`POST /oauth/token`)           |
| Resource Server      | Omnigent server (existing `/v1/**` APIs)        |
| Client               | The headless process, holding the client secret |
| Resource Owner       | The client itself — no third party              |

## Configuration

One confidential client, read from the environment at mount. No table, no
migration: a second client would need a real registry, and the single-client
case covers "this deployment has an automation identity".

| Variable | Meaning |
|----------|---------|
| `OMNIGENT_MACHINE_CLIENT_ID` | The client identifier presented at the token endpoint. |
| `OMNIGENT_MACHINE_CLIENT_SECRET_HASH` | The client secret in **stored form only** — its `hash_secret` digest (HMAC-SHA256 keyed by `cookie_secret`, so 64 hex characters). The raw secret never reaches the server's config, and a value that is not that digest's shape is refused at startup rather than 401ing forever. Generate the secret with `secrets.token_urlsafe(32)` or equivalent: because only the digest is configured, the server cannot measure the secret's entropy, so that part is operator discipline — see row 3 of the threat table for the half that *is* enforced. |
| `OMNIGENT_MACHINE_SUB` | The machine principal the minted token acts as (the `sub` claim). Must be a distinct, non-admin identity. |
| `OMNIGENT_MACHINE_TOKEN_TTL` | Access-token lifetime in seconds (default 3600, and **capped** at 3600). |

All three of the first group must be set to enable the grant, or all unset to
leave it off. **All unset is the default and leaves `POST /oauth/token`
unrouted** — the config is the opt-in, so the endpoint's existence is itself
the signal that a deployment opted in. (The device grant needs a separate
`OMNIGENT_DEVICE_GRANT_ENABLED` because its endpoints are useful with zero
config; this grant has nothing to serve without a configured client.)

Anything else — two of three, a malformed secret hash, a reserved principal,
an unparseable / non-positive / over-ceiling TTL — raises at startup. A
half-configured deploy that came up anyway would look like a client bug from
the outside; failing to start puts the error where the operator can act on it.

The TTL ceiling is enforced, not advised: expiry is this grant's only
revocation (below), so the TTL is the whole bound on a stolen token. 3600s
also matches the device grant's fixed access-token lifetime.

`cookie_secret` is the shared root of trust: it keys both the stored
secret-hash comparison and the signature on issued tokens. Rotating it
therefore invalidates the configured hash **and** every live machine token —
the startup log says so where an operator will see it.

## Flow

```
  client                                    server
    |  POST /oauth/token                      |
    |  grant_type=client_credentials          |
    |  + Basic base64(id:secret)  ─────────►  |  constant-time id + secret-digest check
    |                                         |  mint_delegated_token(sub, scope, no grant_id)
    |  ◄─── {access_token, Bearer, expires_in}|
    |                                         |
    |  Authorization: Bearer <jwt>  ────────► |  path allowlist, no cache, no denylist lookup
```

Client authentication accepts either RFC 6749 §2.3.1 form: HTTP Basic (which
takes precedence) or `client_id` / `client_secret` form fields. Both halves of
the comparison always run and are combined without short-circuiting, so a
failure leaks nothing through timing about which half was wrong. Per §2.3.1 the
two halves of a Basic credential are form-urlencoded before the base64, so they
are urldecoded after the split on `:` (a secret containing `:`, `%`, `+` or a
space would otherwise be read wrong); the scheme token itself is matched
case-insensitively per RFC 7235 §2.1.

The endpoint is throttled per source IP — the same in-memory sliding-window
limiter the device grant applies to its public authorize endpoint, now shared in
`omnigent/server/routes/_oauth.py`. It gates the endpoint ahead of the
credential comparison, not just failed authentications: a limiter that only
counted failures would still answer the guess that happened to be right, so the
guess *rate* is the thing bounded. Ten requests per minute per IP is far above
honest use, since a machine client mints once per token TTL.

Error shapes are the RFC's: any other `grant_type` is `unsupported_grant_type`;
an absent, malformed, or mismatched client is `401 invalid_client`; a client
that authenticates but whose principal no longer passes the admin check is
`403 unauthorized_client`; a source over the throttle is `429 slow_down` (the
shape RFC 8628 §3.5 registers, and what the device grant's throttle already
answers with). There is no "configured but inactive" answer — an unconfigured or
refused grant is unmounted, so the path simply does not exist.

Every response from the endpoint carries `Cache-Control: no-store` and
`Pragma: no-cache` (RFC 6749 §5.1, and §5.2 for the error shape). A `401
invalid_client` that rejected an `Authorization` header also carries the
`WWW-Authenticate: Basic` challenge §5.2 requires; form credentials get no
challenge, since there is no header attempt to retry.

## Token shape

The minted token is the delegated JWT the device grant already defines, with
one difference: the `scope` claim is set and `grant_id` is **absent**.

- `scope` puts the token under the fail-closed path allowlist
  (`delegated_path_allowed`), so it can never reach admin or user-management
  endpoints, and keeps it out of the token-keyed credential cache — a cached
  entry replayed on a non-allowlisted path would skip the allowlist.
- No `grant_id` means no store-backed grant and so no per-token revocation
  lookup. Revocation here is by expiry and secret rotation; a deployment that
  needs immediate, per-token kill should use the store-backed device grant.
- `act` carries `{"client_id": ...}` so every action stays attributable to the
  configured client.

## Security analysis

| # | Threat | Mitigation |
|---|--------|-----------|
| 1 | Leaked client secret → token minting | The secret is a fixed operator config held only by the client, never user-supplied and never in a browser. Rotation is a config change on both sides; the stored form is a keyed digest, so a config leak alone does not yield a usable secret. |
| 2 | Server-side config leak → secret recovery | Only the `hash_secret` digest is configured; the raw secret is never stored or logged. |
| 3 | Credential probing / timing oracle | Client id and secret digest are compared with `hmac.compare_digest` and combined non-short-circuiting; every failure is the same `401 invalid_client`. The endpoint is also unauthenticated by definition, so it is rate-limited per source IP ahead of the comparison (`429 slow_down`), bounding the guess rate. The secret's own entropy is not machine-checkable — only its digest is configured — so that half is stated as operator guidance in the configuration table above rather than pretended to be enforced. |
| 4 | Token used beyond its purpose | Fail-closed path allowlist on every request (no cache shortcut), plus an enforced TTL ceiling and no refresh token. |
| 5 | **Privilege escalation through an admin principal** | The allowlist confines *paths*, not privilege: `/v1/sessions` grants `LEVEL_OWNER` to any `is_admin` identity, so an admin `sub` would own every tenant's session. The permission store is asked whether the configured `sub` is an admin at mount (the router is not built if so) **and again before every mint** (`403 unauthorized_client`), so promoting the principal later stops new tokens without a restart. Both checks fail closed if the store cannot answer, and the refusal names which of the two it hit, so a store outage is not misread as a bad `sub`. Tokens already issued to a since-promoted `sub` live out their TTL — bounded by row 7's ceiling. |
| 6 | Identity confusion with a human account | Operator discipline, with the machine-checkable part enforced: a reserved sentinel (`local`, `__public__`) is refused outright, and the enable log names the `sub`. Whether a non-reserved `sub` collides with a human account is not reliably answerable through the permission store (`is_admin` cannot distinguish absent from non-admin, and `list_users` is paginated), so this doc says it instead of pretending to check it: **give the machine client a fresh, dedicated identity** so audit stays legible and it inherits no human's grants. |
| 7 | Stolen token cannot be revoked | Accepted, and bounded: the TTL is capped at 3600s (`OMNIGENT_MACHINE_TOKEN_TTL` above that refuses to start), and secret rotation stops re-minting. Per-token revocation is the store-backed device grant's model, not this one. |
| 8 | Cookie-secret rotation silently breaking auth | One key underpins both the secret check and token signing; the startup log states the coupling so rotation is not debugged twice. |
| 9 | Token cached by a proxy or client | Every token-endpoint response sends `Cache-Control: no-store` + `Pragma: no-cache` (RFC 6749 §5.1), as does the device grant's `POST /oauth/device/authorize` 200, whose `device_code` + `user_code` are as sensitive (RFC 8628 §3.2). The shared header mapping is read-only, so a caller cannot mutate the constant every grant router hands to its responses. |

### Deliberate limits

- **One client.** A second confidential client needs a persisted registry with
  its own lifecycle; the env-configured single entry covers the automation
  identity case without one.
- **Shared scope with the device grant.** `scope="sessions"` reuses the
  existing delegated allowlist, which also covers `/v1/agents`, `/v1/hosts` and
  `/v1/runners`. Narrowing it per grant would change the
  `delegated_path_allowed` contract both grants read, so a machine token
  inherits whatever those prefixes hold — the same surface the device grant
  already exposes. Confining it further is the per-grant-scopes follow-up
  below; this grant adds no new reachable path.
- **No refresh token.** The client re-mints; there is nothing to rotate or
  replay-detect.
- **In-process throttle.** The limiter is per replica and keyed by source IP,
  the same shape and the same limits the device grant already runs on. A
  multi-replica deployment multiplies the ceiling by the replica count, and a
  distributed guesser spreads across source IPs; a shared-store limiter is the
  fix if that ever matters. It is a rate bound, not the security boundary —
  that is the secret's entropy, which the server cannot check because only the
  digest is configured.

## Out of scope / follow-ups

- A persisted multi-client registry with per-client scopes and an admin UI.
- Per-grant scopes, so a machine token can be confined more tightly than the
  shared session-API allowlist.
- Asymmetric client authentication (`private_key_jwt`, RFC 7523) for
  deployments that would rather not distribute a shared secret at all.
