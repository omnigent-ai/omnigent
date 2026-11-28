# Machine Auth — JWT Bearer Grant (RFC 7523 §2.1)

> **IMPLEMENTED.**
>
> A headless client that holds an access token from a trusted identity provider
> exchanges it for a delegated, path-scoped Omnigent token acting as the
> principal configured for that client. The identity provider keeps the client
> secret, its rotation and its revocation.
>
> Server: `omnigent/server/routes/jwt_bearer.py`. Like the client-credentials
> grant (`designs/CLIENT_CREDENTIALS.md`) it owns no route: it is a `grant_type`
> branch of the one `POST /oauth/token` in `create_oauth_token_router`, and it
> mints with `mint_delegated_token` / `DELEGATED_SCOPE`, so the auth layer's
> existing `scope` confinement applies unchanged. Opt-in and default-off, in the
> cookie-based auth modes (`oidc` and `accounts`).
>
> Tests: `tests/server/test_jwt_bearer.py`,
> `tests/server/integration/test_jwt_bearer_e2e.py`.

## Problem

The client-credentials grant serves one machine client whose secret Omnigent
holds as a keyed digest. Deployments that already run an identity provider for
login usually manage automation identities there too: one client per pipeline,
secrets rotated and revoked centrally, audit in one place. Registering each of
those clients again in Omnigent duplicates that lifecycle, and the single
configured client cannot tell two pipelines apart.

Accepting the provider's access tokens directly on every request would be a
second authentication model beside the delegated one, with its own caching,
confinement and revocation rules. This grant instead exchanges the provider's
token once and issues the existing delegated token shape.

## Role mapping

| RFC 7523 role        | Here                                                     |
|----------------------|----------------------------------------------------------|
| Issuer               | The identity provider (`OMNIGENT_JWT_BEARER_ISSUER`)     |
| Authorization Server | Omnigent server (`POST /oauth/token`)                    |
| Resource Server      | Omnigent server (the delegated `/v1/**` APIs)            |
| Client               | The headless process, holding the provider's token       |
| Principal            | The Omnigent identity configured for that client         |

## Configuration

| Variable | Meaning |
|----------|---------|
| `OMNIGENT_JWT_BEARER_ISSUER` | Exact `iss` of accepted assertions. Required. |
| `OMNIGENT_JWT_BEARER_AUDIENCE` | The `aud` assertions must carry, naming this server. Required. |
| `OMNIGENT_JWT_BEARER_CLIENTS` | Comma-separated `client=principal` pairs. Required. |
| `OMNIGENT_JWT_BEARER_CLIENT_CLAIM` | Claim naming the client. Default `azp`; `client_id` for RFC 9068 tokens. |
| `OMNIGENT_JWT_BEARER_JWKS_URI` | The issuer's JWKS. Defaults to the login provider's when the issuer is the login issuer. |
| `OMNIGENT_JWT_BEARER_TOKEN_TTL` | Ceiling on the issued token's lifetime, in seconds. Default and maximum 3600. |

With every variable unset the grant is off and answers
`unsupported_grant_type`. Any other incomplete or malformed combination refuses
to start, so a typo cannot leave the grant silently disabled.

At the provider, give each pipeline its own confidential client with the
client-credentials grant, and add the configured audience to its access tokens
(for Keycloak, an audience mapper or a client scope carrying it). Then list the
client against a dedicated principal, for example
`ci-triage=ci-triage@ci.invalid`.

## Flow

```
headless client          identity provider               Omnigent
      │ client_credentials        │                          │
      │──────────────────────────►│                          │
      │ ◄──── access token ───────│                          │
      │  (iss, aud, azp, exp)     │                          │
      │                                                      │
      │ POST /oauth/token                                    │
      │ grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
      │ assertion=<access token>                             │
      │─────────────────────────────────────────────────────►│
      │                              verify signature (JWKS),│
      │                              iss, aud, exp, iat,     │
      │                              lifetime ≤ 1h;          │
      │                              azp → principal;        │
      │                              principal not admin     │
      │ ◄──── delegated token (scope, act.client_id) ────────│
      │                                                      │
      │ Authorization: Bearer <delegated token>              │
      │─────────────────────────────────────────────────────►│ allowlist only
```

The issued token expires at the earlier of the assertion's `exp` and the
configured ceiling. The client repeats both steps to renew; there is no refresh
token.

## Token shape

The issued token is the client-credentials shape: `sub` is the configured
principal, `scope` is `DELEGATED_SCOPE`, there is no `grant_id`, and
`act.client_id` names the provider client. The auth layer therefore confines it
to `delegated_path_allowed`, skips the revocation lookup, and never writes it
to the credential cache. The assertion itself is not an Omnigent credential:
presented as a bearer it fails the HS256 check like any foreign token.

## Security analysis

| # | Threat | Mitigation |
|---|--------|-----------|
| 1 | Token minted for another relying party replayed here | `aud` is required and must equal the configured audience; `iss` must match exactly. |
| 2 | Forged assertion | Only asymmetric algorithms are accepted, so the published public key cannot double as an HMAC secret; `none` is refused. The JWKS key's own `alg`, when published, must match the header. `exp`, `iat`, `iss` and `aud` are required. |
| 3 | **A provider-side claim names someone else** | The principal comes only from the server's client mapping, keyed by the client claim the provider sets from its client registration (`azp`). Claims a client or user can influence — `email`, `sub`, custom mappers — never select the principal, and an unlisted client is refused. Anyone able to register clients at the provider still controls which client is which, but cannot reach a principal the server did not map to their client. |
| 4 | Long-lived or replayed assertion | An assertion valid for more than an hour (`exp - iat`) is refused, and the issued token never outlives the assertion. An assertion can be exchanged repeatedly until it expires; each exchange yields the same principal and scope, so replay adds no authority. Single-use `jti` tracking would need shared state across replicas and is not done. |
| 5 | Revoking a client at the provider | The provider stops issuing assertions; every issued token expires no later than the last assertion, so revocation takes effect within the provider's access-token lifetime. Removing the client from `OMNIGENT_JWT_BEARER_CLIENTS` stops exchanges on restart. |
| 6 | **Privilege escalation through an admin principal** | As in the client-credentials grant, the allowlist confines paths, not the `is_admin` → `LEVEL_OWNER` override inside them. Each principal is vetted at mount and before every exchange (`400 unauthorized_client`), failing closed if the check cannot answer. The vetting also consults the admin list: a listed identity is promoted to admin by its first identity lookup, so it is refused before it ever is. |
| 7 | Principal shared with a human | Reserved identities are refused. At mount the server warns when a principal is an existing account or lies in an allowed login domain. Use dedicated principals. |
| 8 | Key-fetch amplification | A forged `kid` triggers at most one JWKS refetch per 30 seconds. Fetches run off the event loop with a short timeout; an unreachable JWKS refuses the exchange. Rotated keys are picked up by that refetch. |
| 9 | Token cached by a proxy | Responses carry `Cache-Control: no-store`. |
| 10 | **Unauthenticated requests exhausting a client's throttle** | Exchanges are throttled per verified client after the assertion is verified, not per source address: behind a reverse proxy every caller shares one address. Rejected assertions spend no budget, so neither invalid requests nor another client can lock a client out. A throttled exchange answers `429 slow_down` with `Retry-After`. |

### Deliberate limits

- **Environment registry.** Clients are configured, not stored; adding or
  removing one is a restart. The provider is where client lifecycle lives.
- **One issuer.** Multiple trusted issuers would need per-issuer audiences and
  keys; one covers the deployment's own provider.
- **Shared scope.** The issued token reaches the same delegated allowlist as
  the device and client-credentials grants.

## Out of scope / follow-ups

- Optional single-use `jti` enforcement with a shared store.
- Per-client scopes, together with the per-grant scopes follow-up in
  `designs/CLIENT_CREDENTIALS.md`.
