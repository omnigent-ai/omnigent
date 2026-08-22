# Machine Client Store (design)

**Status:** Draft, for maintainer input
**Relates to:** #3977 (the env-configured stop-gap this replaces)

## 0. Problem

Omnigent can mint a token for a machine principal, and it can only ever know
about one of them. #3977 added an OAuth 2.0 client-credentials grant whose
confidential client is three environment variables, `OMNIGENT_MACHINE_CLIENT_ID`,
`OMNIGENT_MACHINE_CLIENT_SECRET_HASH` and `OMNIGENT_MACHINE_SUB`. That was the
right size for the case that prompted it, one automation identity on one
deployment, and it is what I have been running on my hosted stack to cover
GitHub Actions and some on-call automation.

Two things about that shape do not survive contact with a second consumer.

A machine client is a principal, and every other principal in omnigent lives in
the database partitioned by workspace. `SqlUser`, `SqlAccountToken` and
`SqlDeviceGrant` all carry `workspace_id` in the primary key, defaulted from
`current_workspace_id`. A client read from the process environment is
deployment-global by construction, so it cannot express a machine identity that
belongs to one workspace rather than to the deployment.

Revocation does not exist. `designs/CLIENT_CREDENTIALS.md` accepts this
explicitly, and the 3600 second ceiling on `OMNIGENT_MACHINE_TOKEN_TTL` is there
because expiry is the only bound on a stolen token. With one shared credential,
revoking a single consumer is not expressible at all. Rotating the secret cuts
off every consumer at once, and the cut takes a redeploy.

## 1. Current Model

Worth stating precisely, because most of it is being kept.

`MachineClientConfig.from_env()` reads the three variables plus an optional TTL
and returns one config or `None`. All unset is the clean off, and leaves
`POST /oauth/token` unmounted. Any other combination raises at startup rather
than coming up with an endpoint that refuses every request.

The secret is stored only as its `hash_secret` digest, HMAC-SHA256 keyed by
`cookie_secret`, and compared with `hmac.compare_digest`. Both the id and the
secret comparison run without short-circuiting, so a failure does not disclose
through timing which half was wrong.

The machine `sub` is vetted by `_vet_machine_sub` at mount and again on every
mint, so promoting that principal to admin stops new tokens instead of waiting
for a restart. Reserved identities are refused outright.

The minted token reuses `mint_delegated_token` with no `grant_id`, which is what
tells the auth layer to skip the revocation denylist. Its `scope` claim confines
it to the same delegated path allowlist the device grant uses.

## 2. What Already Exists

The store this design proposes is the third instance of a settled pattern rather
than a new subsystem.

`DeviceGrantStore` is described in its own module docstring as a sibling to
`SqlAlchemyAccountStore`, same database, separate API surface. It already
implements the parts that are easy to get wrong. Secrets are never stored raw,
`device_code` and each refresh token being kept as `hash_secret` digests.
Single-use redemption is atomic, an `UPDATE ... WHERE ... ` plus a rowcount
check, so a code cannot be redeemed twice or a rotated refresh token replayed
under concurrent requests.

There is also a precedent for an environment-provided credential that does not
own the credential. `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` takes effect on the
first boot of a deployment's accounts database and is ignored with a warning once
an admin exists. Environment bootstraps, the database owns.

## 3. Shape of the Answer

A `machine_clients` table, a `MachineClientStore` beside `DeviceGrantStore`, and
a resolver seam between the store and the token endpoint so the grant handler
does not know where a client came from.

```
POST /oauth/token
  │
  ├─ _presented_client(request, form)        unchanged, RFC 6749 §2.3.1
  │
  ├─ MachineClientResolver.get(client_id)    NEW seam
  │     ├─ StoreResolver   → machine_clients (workspace-scoped)
  │     └─ EnvResolver     → the #3977 variables, first-boot bootstrap only
  │
  ├─ constant-time secret compare            unchanged, hash_secret
  ├─ _vet_machine_sub(matched.sub)           per matched client
  └─ mint_delegated_token(..., grant_id=?)   see D4
```

Everything outside the resolver stays as #3977 built it. The RFC parsing, the
digest comparison, the sub vetting, the per-IP sliding window on the
unauthenticated client check, the delegated token shape, and the standing-down
behaviour when the device grant owns `POST /oauth/token` are all unchanged.

## 4. Decisions

Eight decisions. Each states its position first, then the reasoning that holds it
up.

### D1 Table Shape

`machine_clients`, partitioned like its siblings.

| Column | Notes |
|---|---|
| `workspace_id` | part of the PK, `default=current_workspace_id` |
| `client_id` | part of the PK, the identifier presented at the endpoint |
| `sub` | the principal minted tokens act as |
| `token_ttl_seconds` | per client, same ceiling as today |
| `created_at`, `created_by` | who registered it |
| `disabled_at` | soft disable, so a revoked client id cannot be silently reused |

`workspace_id` earns the column by consistency rather than by immediate use.
Every sibling principal table partitions on it, and a machine client is a
principal, so a table that omitted it would be the one row in the schema that
cannot answer which workspace it belongs to. A deployment that never uses more
than workspace 0 pays a column for that.

`created_by` is bookkeeping, and it becomes load-bearing the moment more than one
person can register a client. Recording it from the start costs nothing and
avoids a backfill against rows whose provenance is already lost.

`client_id` is scoped by workspace rather than globally unique, which follows the
sibling tables and means two workspaces can both register `github-actions`.

Secrets live in a child table rather than a column on the row above.

| `machine_client_secrets` | Notes |
|---|---|
| `workspace_id`, `client_id` | FK to the client |
| `secret_hash` | `hash_secret` digest, never the raw secret |
| `created_at` | when this secret was added |
| `expires_at` | nullable, set when the secret is being retired |

A column would allow exactly one live secret, which makes rotation a cutover.
Rotating then means registering a second `client_id` and retiring the first, so
the client's identity changes underneath its consumers and the audit trail splits
across two ids for one automation. With a child table, a rotation is additive.
Register a second secret, both authenticate, migrate the consumer, set
`expires_at` on the old one. The identity is stable throughout and an operator
can see when each secret entered service.

Authentication accepts any unexpired secret for the client. D3's constant-time
requirement then applies across the set rather than to a single digest.

### D2 Stored Secret Form

`hash_secret` keyed by `cookie_secret`, unchanged. It is already the repository's
one stored-secret convention and it already covers the machine client. The raw
secret is returned once at registration and never persisted.

One key underpins the secret check and token signing, so rotating `cookie_secret`
invalidates every stored machine secret at the same time it invalidates sessions. `designs/CLIENT_CREDENTIALS.md` already logs that
coupling at startup.

### D3 Constant-Time Lookup

The presented secret is hashed once on every request and compared whether or not the
`client_id` resolves, against a fixed dummy digest when it does not.

#3977 runs both comparisons unconditionally, and a store lookup gives that property
away if written the obvious way. An unknown `client_id` returns before the HMAC
while a known one pays for it, which makes client ids enumerable by timing. Hashing
regardless costs one HMAC per request either way.

Across D1's secret set the presented digest is compared to every live secret with
`hmac.compare_digest`, results combined without short-circuiting, so the answer does
not depend on which secret matched or the order they were checked. The single HMAC
dominates, and the residual signal is how many live secrets a client holds, which is
one or two during a rotation window and is not sensitive.

### D4 Revocation Scope

Machine tokens stay offline-validated and carry no `grant_id`. Revocation of
minting is the store row. Bounding an already-issued token is the per-client
`token_ttl_seconds` from D1, floored near 600 seconds, with the global ceiling left
at 3600.

**One. Two different things are called revocation.** Disabling a client row stops
the next mint the moment it lands, because the mint is where the client re-presents
its secret and the row is read. Ending a token already issued is a separate
question, bounded by the life that token has left. The store delivers the first
outright, and the second is what this decision sizes.

**Two. The mint is already a checkpoint.** A device client holds a rotating refresh
token for up to 30 days and never re-proves who it is, so nothing between issue and
expiry makes the server reconsider the grant. A per-request denylist read is the
only checkpoint available to it. A machine client re-presents its secret on every
mint, so the interval between checkpoints is the TTL, and the TTL is a number an
operator sets. The device grant's storage disciplines are worth copying and D1
through D3 copy them. Its `grant_id` claim and refresh machinery answer a
consent-and-no-credential problem a machine client does not have.

**Three. The interval belongs to the client, not the deployment.** D1 gives every
client its own `token_ttl_seconds`. A principal that reaches something consequential
takes a minutes-scale token, which bounds the residual where the exposure is, while
a high-volume CI client keeps a longer one and the outage tolerance that comes with
it. A single global ceiling cannot express that difference, so lowering one spends
every client's resilience on one client's exposure.

**Four. The floor is load-bearing.** Below roughly 600 seconds the token stops
being a credential and becomes a liveness dependency, for three reasons that arrive
together.

- The mint endpoint is rate limited per source IP, so the sustainable client count
  behind one egress address is about `10 * TTL / 60`. At 300 seconds that is
  50 clients with no headroom for a burst, and a fleet behind one NAT gateway is
  ordinary rather than exotic.
- Clients that deploy together renew together, and cron-driven CI is phase-locked to
  the hour. A shorter TTL does not spread those mints. It multiplies how often the
  fleet meets the limiter and shrinks the slack left to retry before the token
  expires.
- The TTL is also an availability buffer. A token needing no store read survives an
  outage for its remaining life, so a 20 minute job that minted once rides out a
  restart at 3600 seconds and fails mid-run at 300.

**Five. Three things ship with it.** Renewal jitter, so a fleet refreshing at a
spread fraction of the TTL does not synchronise against the limiter. The
authenticated mint limiter keyed on `client_id` rather than source IP, since once
the secret is proven the address constrains the wrong party, missing an attacker who
can move and penalising co-located clients who cannot. And `jti` with
`act.client_id` logged at mint and on served requests, because without a per-request
store read the server holds no inventory of outstanding tokens, so the claim is only
traceable if it reaches a log. Repeated use of one `jti` from an unexpected source
is the detection signal that remains.

**Six. The denylist is declined.** Giving machine tokens a `grant_id` so the
existing revocation check applies reads as reuse and costs more than it appears to.

- It is a store read on every authenticated request, on a path with no cache to
  absorb it. A scope-carrying token is never cached, because the cache is
  token-keyed and a replayed entry on a non-allowlisted path would skip the
  allowlist. The read therefore lands on every call from a job that mints once and
  calls many times.
- The failure policy would be invented rather than inherited. `is_revoked` has no
  error handling and neither does the auth layer above it, so a store error becomes
  a 500 instead of a 503 carrying a retry signal, and it arrives without the
  timeout, the circuit breaker, or the off-loop execution that the permission reads
  already have.
- It buys down the narrower threat. Against a leaked secret it adds nothing, since
  the attacker mints until the row is disabled and disabling the row stops minting
  either way. It wins only where an issued token leaked while its secret did not,
  and only where detection beats the TTL. For a client whose token derives from a
  secret sitting beside it, that is a thin slice.
- It cannot be added by setting a claim. The revocation hook is unwired in every
  deployment that has machine clients, which D8 covers.

If a per-token kill becomes a stated requirement, the answer is a normally-empty
in-memory list of specific `jti` values, self-evicting at each token's expiry, which
costs nothing in the steady state and does not put a read on the request path.

### D5 Env as Bootstrap

The #3977 variables keep working and stop being the source of truth. On first
boot, if they are set and the workspace has no machine clients, they seed one row
and log that they did. Once a row exists they are ignored with a warning, exactly
as `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` behaves.

This keeps a headless deploy working with no registration step, keeps my hosted
stack running across the change, and avoids a config surface that has to be
deprecated separately.

The seeded row lands in workspace 0 explicitly rather than wherever
`current_workspace_id` happens to resolve, because a default is not a decision and
a bootstrap that picks a tenant by accident is worse than one that refuses. A
deployment with more than one workspace should register through D6 instead, and
relying on the bootstrap there is an operator error rather than a supported path.

### D6 Registration Surface

Registration is restricted to workspace and deployment operators, and the existing
`is_admin` boolean is the gate.

No finer gate exists. Omnigent has no role primitive. What exists is `is_admin` on `SqlUser`,
set through `PermissionStore.set_admin` and promoted from the operator-editable
admin list, and a per-resource ladder in `session_permissions` with
`LEVEL_READ` through `LEVEL_OWNER` keyed by conversation. The ladder is session
sharing, not capabilities, so there is no "may manage machine clients" permission
to grant and no way to express one without either stretching a conversation-scoped
level onto a non-conversation resource or introducing the codebase's first roles
table. Either of those is a larger design than the registry it would serve, so the
registry should not be gated on it. Admin-or-not is the honest ceiling for now, and
it is sufficient, because registering a machine client is an operator action.

The surface is a CLI command first, and an admin HTTP route is a follow-on that
reuses the same check. The CLI needs no new HTTP surface and no new guard, so it is
the shorter path to a usable registry.

Two constraints come with it. The admin check is currently copy-pasted, defined
privately as `_require_admin` in both `sharing.py` and `default_policies.py` and
inlined in three more spellings across `auth.py` and `accounts_auth.py`, so a
registry route extracts a shared dependency rather than adding a sixth. And
whatever path it lands on sits outside
`delegated_path_allowed` (`auth.py`), or a machine token could register machine
clients. The primary guard against that is already in place, since the machine
`sub` is vetted non-admin at mount and on every mint and therefore fails an admin
check regardless, but path placement is the second layer and should be deliberate
rather than incidental.

### D7 Sub Sharing

Two clients may point at the same `sub`, and the store warns rather than refuses.

Attribution is the apparent cost and it does not apply. `mint_delegated_token`
already sets an `act` claim, RFC 8693 provenance carrying the `client_id`, so which
client acted is recorded independently of which principal it acted as. A shared
`sub` therefore keeps per-consumer attribution, provided the audit path reads
`act.client_id` rather than `sub`. That obligation falls on whoever consumes the
trail, and it is not visible from the schema.

What allowing it buys is a second rotation story alongside D1's. Overlapping
secrets on one client covers rotating a credential. A second client on the same
`sub` covers migrating a consumer, standing up a new automation against the same
principal and retiring the old one on its own schedule, without either of them
sharing a secret.

### D8 Revocation Hook Safety

A `grant_id` that cannot be checked is rejected rather than honoured, and a grant
type that mints one refuses to start unless its check is wired. This is worth
doing whether or not D4 ever changes, because the current arrangement fails open.

`set_grant_revocation_check` has one caller, and it sits inside the device-grant
block behind `OMNIGENT_DEVICE_GRANT_ENABLED`, an `accounts` source, and a
permission store. This grant mounts under `oidc` or `accounts` and stands down
whenever the device grant owns the token endpoint, so the two are mutually
exclusive. Every deployment that has machine clients is therefore a deployment
where the hook was never installed.

What that costs is hidden by the shape of the check. The auth layer consults the
denylist only when the callable is present, so an absent one skips the clause and
returns a valid identity. A `grant_id` added to a machine token in that deployment
would take the delegated branch, pass the path allowlist, skip the revocation
lookup, and authenticate. The schema, the operator tooling, and the token would all
say revocable while nothing revoked anything.

Three changes close it.

- Reject a token carrying a `grant_id` when no check is wired. Carrying the claim
  is an assertion of revocability, and serving it unchecked is a false one. This
  turns a silent condition into an immediate and loud one.
- Assert at boot that any grant type minting `grant_id` tokens has a check wired,
  so the failure lands at startup rather than on the first request nobody audits.
- Route the check by grant type instead of through a single global callable. Two
  grants sharing one hook means the second registration overwrites the first, and
  the surviving store answers "unknown" for the other's identifiers, which
  fail-closed turns into every one of that grant's tokens being rejected.

A regression test is necessary and not sufficient here. The defect is an absent
wiring in one deployment topology, and the next contributor to add a grant will not
think to write the test that catches it.

## 5. Delivery Slices

Each reviewable on its own, and each leaves the tree working.

1. Both tables from D1, their models, and the migration. No behaviour change.
2. `MachineClientStore` with the atomic-update idiom, plus its tests.
3. The resolver seam, backed by the existing env config. Pure refactor of
   #3977, no new capability, and the point at which the endpoint stops caring
   where a client came from.
4. The store-backed resolver, with D3's constant-time lookup.
5. The first-boot bootstrap of D5.
6. The registration CLI of D6, and with it the extraction of a shared admin-check
   dependency so this is not the sixth private copy.
7. D4's companions, which are the per-client TTL floor, renewal jitter, keying the
   authenticated mint limiter on `client_id`, and the `jti` logging that gives a
   leaked token a trail.

D8 is not in that order because it does not depend on any of it. The revocation
hook fails open today, so tightening it stands alone and could land before slice 1
or beside it.

Slice 3 is the one worth landing early even if the rest waits. It is a
behaviour-preserving refactor of code that is already reviewed, and it makes the
env-versus-store decision reversible.

## 6. Still Open

- Rotating `cookie_secret` gets harder as the registry grows, and this design does
  not solve it. D2 keeps the existing stored form, so one key underpins both the
  stored secret check and token signing. The sharp edge is that a keyed digest
  cannot be re-keyed. Recomputing it under a new key needs the raw secret, which is
  the one thing never stored, so a rotation does not migrate the registry, it
  invalidates it. Every client needs a newly issued secret installed inside the
  same window, and D1's child table cannot stage that because the new-key rows would
  need raw secrets nobody holds. The device grant inherits the same coupling and
  tolerates it, since a user whose grant died simply logs in again. Machine clients
  do not self-heal. Whether they should therefore diverge from the repository's one
  stored-secret convention, either by keying on something rotatable independently of
  session signing or by dropping the pepper for a salted hash, is the one question
  here with no sibling answer to copy.
- Per-client scopes stay out. The `scope` claim resolves through the shared
  delegated allowlist that the device grant also reads, so narrowing it per client
  changes a contract both grants depend on. The cost of deferring is that every
  machine client gets the full allowlist, which reaches `/v1/sessions`,
  `/v1/agents`, `/v1/hosts` and `/v1/runners`, so a client that only creates
  sessions is broader than it needs to be. That is acceptable while clients are
  operator-registered, and it is its own design when they are not.

## A. Rejected Alternatives

**A JSON list in an environment variable.** Multiple clients without a migration.
It fails on the two things that motivate the change. The clients stay deployment-global, so a workspace-scoped machine
identity is still not expressible, and revocation is still a redeploy. It also
sits against the repository's own convention, where environment variables bootstrap
principals and the database owns them.

**A mounted JSON or YAML credentials file.** Better than the environment variable
for structured multi-entry secrets, and it is what a service whose credentials are
purely operator-editable would use. Same two failures as above.

**Dynamic Client Registration, RFC 7591.** The standard answer for programmatic
client creation, and the right one for a deployment acting as a general-purpose
authorization server. Omnigent is an application with an automation identity, so
the endpoint, its access-token policy, and its client-metadata surface are all
scope that no request has asked for.

**`secret_hash` as a column on the client row.** One fewer table, and it is what
#3977 effectively has today. It allows exactly one live secret, which makes every
rotation a cutover with no window where both the old and new credential work. The
smaller variant of D1's child table is two columns, a current and a previous
digest, which does buy an overlap window but cannot express when the previous one
stops being accepted. Retiring a secret then means remembering to clear a column
rather than setting a date, so the child table is worth the migration.

**Extending `session_permissions` to gate registration.** The levels ladder
(`LEVEL_READ` through `LEVEL_OWNER`) is the only permission machinery finer than
`is_admin`, so reaching for it is tempting. It is keyed by conversation, and a
machine client is not one, so this would mean either a sentinel conversation id
standing in for the whole workspace or a second meaning for a column that already
has one. `is_admin` is coarser and honest.

**Reusing `SqlDeviceGrant` for machine clients.** They share a store pattern, not
a lifecycle. A device grant is created by a human consenting in a browser and
carries a user, a status, and rotating refresh tokens. A machine client is
registered by an operator, has no consent step, and has no refresh token. Folding
them into one table would mean columns that are null for half the rows and a
status enum meaning two different things.
