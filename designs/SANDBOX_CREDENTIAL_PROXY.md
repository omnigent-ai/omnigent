# Secretless Credential Proxy

> **IMPLEMENTED.** Config surface: `os_env.sandbox.credential_proxy`.
> Code: `omnigent/inner/credential_proxy.py`,
> `omnigent/inner/egress/proxy.py`, `omnigent/inner/egress/aws_sigv4.py`,
> `omnigent/spec/parser.py`.

## Problem

A sandboxed tool often needs to authenticate to an external host —
`gh api`, `git clone https://github.com/...`, a Bearer-token SaaS API.
The naive approach injects the real token into the sandbox env (or a
config file the sandbox can read). That defeats much of the point of
the sandbox: any code the agent runs — including code it was tricked
into running by a prompt-injection payload in a fetched web page or a
malicious dependency — can read the token out of `os.environ` /
`~/.gitconfig` and exfiltrate it to an attacker-controlled host that
happens to be on the egress allow-list (or over a covert channel).

We want tools inside the sandbox to be able to authenticate **without
the real secret ever entering the sandbox**.

## Approach

The L7 egress proxy is already a mandatory MITM for all HTTP(S) traffic
leaving the sandbox (see the egress allow-list machinery in
`omnigent/inner/egress/`). We extend it to attach credentials. The
default model is **swap-on-access**: nothing credential-shaped enters
the sandbox at all.

1. **Parent resolves the real secret.** When the helper starts
   (`_HelperProcessClient._start_locked`), the parent — which is *not*
   sandboxed — resolves each configured secret from its source
   (`{env: ...}`, `{file: ...}`, or `{command: ...}`). The real secret
   stays in the parent and the proxy's in-memory rewrite table.

2. **The egress proxy injects on access (default).** A tool simply
   makes its request to the bound host with **no `Authorization`
   header**. The proxy recognises the bound host and injects
   `Authorization: <scheme> <real>` on the way out. `git clone`,
   `curl`, `python`, `node` — any HTTP client — authenticate with zero
   in-sandbox wiring. The sandbox holds nothing to leak.

3. **Opt-in placeholder injection for credential-gating clients.** Some
   clients refuse to issue a request when they don't see a credential
   locally — most notably `gh`, which short-circuits with
   "authentication required" *before* touching the network, so there is
   no outbound request for the proxy to decorate. For those, an entry
   sets `env:` (e.g. `GH_TOKEN`). The parent mints a random, single-use
   placeholder prefixed `oa_cred_` (`secrets.token_urlsafe`) and injects
   *only the placeholder* into that env var. The client believes it is
   authenticated, issues the request carrying the placeholder, and the
   proxy swaps it. The placeholder is non-secret and bound to one host.

4. **Leak guard (placeholder path only).** A placeholder presented for a
   host it is not bound to (an exfiltration attempt) is rejected with
   HTTP 403; an unknown `oa_cred_*`-shaped value is likewise rejected. So
   even if a tool reads the placeholder out of its own env and replays it
   against an attacker host, the proxy attaches no real credential. (Pure
   swap-on-access entries inject no placeholder, so there is nothing in
   the sandbox to replay in the first place.)

5. **No clobbering.** A real, non-placeholder `Authorization` header the
   client set itself is forwarded untouched and suppresses injection, so
   the proxy never overwrites an unrelated credential a tool deliberately
   sent.

```
  Default — swap-on-access (nothing in the sandbox):

   parent (unsandboxed)               sandbox                 upstream
   ────────────────────               ───────                 ────────
   resolve real secret
   (held in proxy table)     git/curl → GET /repo (no auth)
                                                    │
                               egress MITM proxy ◀──┘
                               host bound? inject Authorization: <scheme> <real>
                                                    └────────────────▶  200 OK

  Opt-in — placeholder injection (gh-class clients):

   resolve real secret
   mint  oa_cred_XXXX  ──inject GH_TOKEN──▶  gh builds
                                             Authorization: token oa_cred_XXXX
                                                    │
                               egress MITM proxy ◀──┘
                               verify host binding; swap → token <real>  ──▶ 200 OK
                               (wrong host → 403)
```

## YAML surface

All entries live in a `credential_proxy:` list under
`os_env.sandbox`. The block **requires** `egress_rules` and a
hard-isolating backend (`linux_bwrap` or `darwin_seatbelt`) — the
parser rejects it otherwise, because only those two backends can
guarantee the MITM proxy is the *only* egress path (a tool can't open a
raw socket around it). The bound host of every entry must also be
reachable under `egress_rules`.

Four types, two generic primitives and two presets. All default to
swap-on-access:

| Type | Wire scheme | Injection | Notes |
|------|-------------|-----------|-------|
| `https_bearer` | `Authorization: Bearer <real>` | swap-on-access (optional `env:`) | Generic Bearer-token SaaS. |
| `https_basic` | `Authorization: Basic b64(user:<real>)` | swap-on-access (optional `env:`) | Generic Basic auth; `username` defaults to `x-access-token`. |
| `git_https` | `Authorization: Basic b64(user:<real>)` | swap-on-access | Preset for git-over-HTTPS; nothing in the sandbox. |
| `gh_basic` | Basic for git host, `token` for api host | swap-on-access for git; `GH_TOKEN`/`GITHUB_TOKEN` env for api | Preset for GitHub CLI + git; defaults to `github.com` + `api.github.com`. |
| `databricks_cli` | `Authorization: Bearer <real>` per workspace host | placeholder `.databrickscfg` file (one `oa_cred_*` per profile) | Preset for the Databricks CLI; takes `profiles` (+ optional `default`). See below. |

Common fields: `target`/`targets` (host + optional path glob — only the
host binds the credential; path scoping is delegated to `egress_rules`),
and `source`, a single-key nested mapping naming where the parent reads
the real secret — `{env: VAR}`, `{file: /path}`, or `{command: ...}`.
`https_*` take an **optional** `env` (the opt-in injection shim — when
present, a synthetic placeholder is injected into that env var);
`https_basic` / `git_https` take an optional `username`.

```yaml
os_env:
  sandbox:
    type: linux_bwrap
    egress_rules:
      - "* github.com/**"
      - "* api.github.com/**"
      - "* mycorp.atlassian.net/**"
    credential_proxy:
      # gh_basic: git host is pure swap-on-access; the api host injects
      # GH_TOKEN/GITHUB_TOKEN because gh gates on a local token.
      - type: gh_basic
        source: {command: gh auth token}
      # git_https: nothing enters the sandbox — git fires its request and
      # the proxy injects Basic auth for the bound host.
      - type: git_https
        target: github.com/databricks-eng/agent-framework.git
        source: {env: OA_TEST_GITHUB_PAT}
      # https_bearer with no `env`: swap-on-access. curl/python send no
      # Authorization header; the proxy attaches Bearer <real>.
      - type: https_bearer
        target: mycorp.atlassian.net/rest/**
        source: {env: JIRA_PAT}
      # https_bearer WITH `env`: opt-in placeholder injection for a
      # client that won't call without a local token.
      - type: https_bearer
        target: gating-saas.example.com
        source: {env: SAAS_PAT}
        env: SAAS_TOKEN
```

The `source` mapping is validated by a small pydantic boundary model
(`_CredentialProxyItemModel` / `_CredentialSourceModel` in the spec
parser) that rejects unknown keys, enforces exactly one source key, and
checks POSIX env-var names — then converts to the `CredentialSourceSpec`
dataclass the runtime consumes.

### `databricks_cli` — profile-keyed, refreshing, file-materialized

The Databricks CLI is a fifth type that differs from the four host-keyed
primitives above:

- **Profile-keyed, not host-keyed.** It takes `profiles: [name, ...]`
  (and optional `default`) instead of `target`/`targets`/`source`. The
  workspace host behind each profile is only known once the parent
  resolves it, so profiles are carried on `DatabricksProxySpec` rather
  than in the host-keyed `entries` list. Only the listed profiles are
  proxied; every other profile is invisible to the sandbox.
- **File materialization, not env injection.** The CLI gates on a local
  credential (like `gh`) and `DATABRICKS_HOST`/`DATABRICKS_TOKEN` carry
  only one workspace, so per-profile selection needs a config file. The
  parent writes a placeholder-only `.databrickscfg` into the sandbox
  scratch dir — one `[profile]` section per profile with the real `host`
  and a synthetic `token = oa_cred_*` — and points `DATABRICKS_CONFIG_FILE`
  at it (and `DATABRICKS_CONFIG_PROFILE` when `default` is set). The CLI
  emits `Authorization: Bearer oa_cred_*`, which the proxy swaps per host.
- **Refreshing secret.** Databricks profiles are usually OAuth
  (`auth_type = databricks-cli`) with a ~1h token, so the rewrite rule
  holds a `DatabricksProfileTokenProvider` (via the SDK) instead of a
  static secret. The proxy calls `rule.resolve_secret()` on each swap; the
  provider re-mints via `Config.authenticate()` at most once per throttle
  window (the SDK caches in-memory and only re-shells near expiry), so a
  long session survives token expiry. The provider requires the
  `databricks` extra and fails loud if it is missing.
- **Egress is operator-listed.** Reaching a workspace requires its host in
  `egress_rules` (`* <host>/**`), the same as every other credential-proxy
  type. The proxy does not widen egress on its own — an earlier draft
  auto-added resolved hosts, but that was dropped to keep egress behavior
  consistent across types (the operator declares every reachable host).
- **Linux only.** The `databricks` CLI is a Go binary and Go on macOS
  ignores `SSL_CERT_FILE`, so `databricks_cli` is rejected on
  `darwin_seatbelt` (same rationale as `gh_basic`); use `linux_bwrap`.

```yaml
os_env:
  sandbox:
    type: linux_bwrap
    egress_rules:
      - "* pypi.org/**"
    credential_proxy:
      - type: databricks_cli
        profiles: [dbc-adb7b1a3-9097, oss]
        default: dbc-adb7b1a3-9097
```

### `aws_sigv4` — full request re-signing, not header substitution

AWS SigV4 can't use swap-on-access or placeholder substitution: the
`Authorization` header is a signature *over the whole request* (method,
canonical URI, canonical query string, a canonical header set, and the
declared payload hash), not an opaque credential string. There is no
single value to inject or swap — the signature has to be discarded and
rebuilt from scratch with the real credential, over the literal bytes
going upstream. `aws_sigv4` is therefore a structurally separate
mechanism from the four types above, though it shares their `source` /
`{env|file|command}` resolution model and their host-keyed, `egress_rules`
-gated shape.

**Always-on placeholder credential, not opt-in.** Every other type
defaults to swap-on-access (nothing enters the sandbox) and only injects a
placeholder for clients that refuse to make an unauthenticated request.
`boto3` can't make a request at all without *some* configured credential,
so `aws_sigv4` always injects placeholder `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` (plus `AWS_DEFAULT_REGION` / `AWS_REGION` /
`AWS_EC2_METADATA_DISABLED=true`) into the sandbox env whenever any
`aws_sigv4` entries are configured. The sandboxed `boto3` client signs its
request with these placeholders — producing a well-formed but
cryptographically garbage `Authorization: AWS4-HMAC-SHA256 ...` header —
and the egress proxy discards that signature entirely and rebuilds it with
the real credential before forwarding.

**Always discard-and-rebuild, not inject-if-absent.** Because `boto3`
always sends *some* SigV4 `Authorization` header, the proxy can't use the
"inject only if the header is absent" swap-on-access rule the other types
use. Instead: a request to a bound host whose `Authorization` value is
SigV4-shaped (`AWS4-HMAC-SHA256 ...`) is always resigned; a request whose
`Authorization` is absent or a different shape is left untouched —
defense-in-depth against clobbering an unrelated credential a tool
deliberately sent.

**The body — and its `X-Amz-Content-Sha256` declaration — are never
touched.** This matters more than it looks: as of the current `botocore`
default (`request_checksum_calculation="when_supported"`), **every** S3
upload (`PutObject`, `UploadPart`, …) is sent `aws-chunked` with
`X-Amz-Content-Sha256: STREAMING-UNSIGNED-PAYLOAD-TRAILER`, regardless of
body size — this is not a size threshold, and reads (`GetObject`,
`HeadObject`, `ListObjectsV2`, …) are unaffected since they carry no body.
It's tempting to assume "chunked" needs special handling — resigning a
chain of per-chunk signatures, the classic SigV4 streaming mode — but that
mode is gone from current `botocore` entirely (no `chunk-signature`/
per-chunk HMAC chain exists in the installed package). The modern
streaming-trailer mode carries **no** cryptographic signature over the
payload at all: it's wire framing (`<hex-len>\r\n<data>\r\n` chunks) plus
a trailing *non-cryptographic* checksum (CRC32 by default) — TLS plus that
checksum cover integrity instead of the SigV4 signature. So the value
already present in `X-Amz-Content-Sha256` — a real hex hash,
`UNSIGNED-PAYLOAD`, or the streaming-trailer sentinel — depends only on
body content, never on which credentials signed the request, and the
client's own value (even though signed with the placeholder keys) is
already correct. The proxy forwards the body completely unchanged and
preserves this header verbatim in every case — chunked and non-chunked
uploads need no special-casing and share the exact same resign code path.
Only presigned query-string auth (`?X-Amz-Signature=...`, a different
signing mode not produced by ordinary `boto3` API calls) is out of scope.

**Three credential shapes**, mutually exclusive:

```yaml
os_env:
  sandbox:
    type: linux_bwrap  # aws_sigv4 also works on darwin_seatbelt — its
                        # signing happens entirely in the parent process,
                        # unlike the Go-CLI-backed types above.
    egress_rules:
      - "* mybucket.s3.us-east-1.amazonaws.com/**"
    credential_proxy:
      # Static credential — access key id + secret key (+ optional
      # session token), each resolved the same {env|file|command} way as
      # every other credential-proxy source.
      - type: aws_sigv4
        target: mybucket.s3.us-east-1.amazonaws.com
        region: us-east-1
        service: s3                                  # optional, default "s3"
        credential:
          access_key_id: {env: AWS_ACCESS_KEY_ID}
          secret_access_key: {env: AWS_SECRET_ACCESS_KEY}
          # session_token: {env: AWS_SESSION_TOKEN}   # optional

      # profile — the parent resolves the full credential from a named
      # profile in its own ~/.aws/config / ~/.aws/credentials, via boto3's
      # Session(profile_name=...). One shared credentials file with many
      # profiles (e.g. "prod", "staging") can back different aws_sigv4
      # entries this way, including a profile that itself role-chains via
      # source_profile or uses SSO — boto3 resolves and refreshes all of
      # that, not omnigent.
      - type: aws_sigv4
        target: staging-bucket.s3.us-east-1.amazonaws.com
        region: us-east-1
        credential:
          profile: staging

      # assume_role (recommended) — the parent mints and auto-refreshes
      # temporary credentials via an explicit STS call, using its OWN
      # ambient AWS identity (env/config/instance-profile/SSO — whatever
      # the omnigent server itself runs as, or a specific named `profile`
      # below) as the caller. No long-lived IAM user key needs to be
      # handed to omnigent when the server already has a role that can
      # assume one.
      - type: aws_sigv4
        target: otherbucket.s3.us-west-2.amazonaws.com
        region: us-west-2
        credential:
          assume_role:
            role_arn: arn:aws:iam::123456789012:role/omnigent-agent-s3
            duration_seconds: 3600
            # profile: prod                           # optional caller identity
```

`region`/`service` are declared explicitly per binding, matching every
other credential-proxy type's explicit-field style, rather than parsed
from the host string. `target`/`targets` work the same as `https_bearer` —
`aws_sigv4` is host-keyed (the bucket/region are known upfront in config),
unlike `databricks_cli`'s runtime-resolved profile keying.

**Multi-region caveat.** `AWS_DEFAULT_REGION`/`AWS_REGION` are single
global env vars — with multiple `aws_sigv4` entries spanning regions, only
the *first* entry's region becomes the sandbox's ambient default. An agent
targeting more than one region should pass `region_name=` explicitly per
`boto3.client(...)` call to hit each entry's exact bound host. A mismatch
fails safe either way: the request lands on an unbound (or
`egress_rules`-disallowed) host and either gets a `403` from the proxy or
a `SignatureDoesNotMatch` from AWS — never a credential leak.

## Internal model

`omnigent/inner/datamodel.py`:

- `CredentialSourceSpec` — `kind` (`env`/`file`/`command`) + the
  corresponding field.
- `CredentialProxyEntry` — the normalized internal shape every YAML
  type compiles down to: `host`, `scheme` (`basic`/`bearer`/`token`),
  `source`, `username | None`, `inject_env: list[str]` (empty for
  swap-on-access; populated only by the opt-in `env` shim).
- `CredentialProxySpec` — list of entries plus an optional
  `databricks: DatabricksProxySpec`; attached to
  `OSEnvSandboxSpec.credential_proxy`.
- `DatabricksProxySpec` / `DatabricksProfileBinding` — the profile list
  (+ `default`, `config_env`) for the `databricks_cli` type.
- `AwsAssumeRoleSpec` — STS `AssumeRole` parameters (`role_arn`,
  `session_name`, `duration_seconds`, `external_id`, optional `profile`
  for the caller identity) for a refreshing `aws_sigv4` credential.
- `AwsSigV4CredentialSpec` — exactly one of: a static 3-part credential
  (`access_key_id` / `secret_access_key` / optional `session_token`, each
  a `CredentialSourceSpec`), `profile` (a named AWS profile resolved via
  `boto3.Session(profile_name=...)`), or `assume_role` — mutually
  exclusive.
- `AwsSigV4ProxyEntry` — the host-keyed `aws_sigv4` binding: `host`,
  `region`, `service`, `credential`. Carried on `CredentialProxySpec.
  aws_sigv4` — a separate list from `entries`, since the proxy enforces it
  through a wholly different mechanism (full re-signing, not header
  substitution).

The parser (`omnigent/spec/parser.py`, `_parse_credential_proxy`)
validates each raw entry with a pydantic boundary model
(`_CredentialProxyItemModel`, which nests `_CredentialSourceModel` for
the `source` mapping) — type, target/targets cardinality, source shape,
POSIX env var names, unknown-key rejection — wrapping any
`ValidationError` as an `OmnigentError`. It then normalizes the four
user-facing types into `CredentialProxyEntry` lists and applies the
checks pydantic can't express: DNS-safe host (reuses
`is_dns_safe_host`), duplicate-host rejection, `egress_rules` present,
backend allow-list, and the `gh_basic`-on-macOS guard.

## Runtime

`omnigent/inner/credential_proxy.py`:

- `prepare_credential_proxy_runtime(spec, parent_env)` runs in the
  parent. For each entry it resolves the real secret and returns a
  `CredentialProxyRuntime` with:
  - `helper_env_updates` — synthetic values for each `inject_env` var
    (empty for swap-on-access entries),
  - `rewrites: list[CredentialRewriteRule]` — `(host, scheme,
    real_secret | secret_provider, synthetic | None, username)` for the
    proxy. `synthetic` is `None` for swap-on-access entries; it is minted
    (and the matching placeholder injected) only when the entry sets `env`
    (or, for `databricks_cli`, per proxied profile). A rule carries either
    a static `real_secret` or a refreshing `secret_provider` — the proxy
    calls `rule.resolve_secret()` — plus, for `databricks_cli`,
    `sandbox_files` (the placeholder `.databrickscfg`).

The real secret lives **only** in the parent process and the proxy's
in-memory rewrite table. It is never serialized into the
`SandboxPolicy` (which can reach logs/dumps), never placed on argv, and
never written to disk in the sandbox. Swap-on-access puts *nothing*
credential-shaped in the sandbox; the opt-in path puts only the
non-secret `oa_cred_*` placeholder.

> **Removed: the git credential helper.** Earlier revisions installed a
> per-host git credential helper inside the sandbox (an `oa_cred_*`-
> returning script wired via `GIT_CONFIG_*`) so `git` over HTTPS would
> emit the placeholder. Swap-on-access makes that unnecessary: `git`
> fires its unauthenticated request and the proxy injects the real Basic
> credential directly. The helper, its config-pipe payload, and the
> `OMNIGENT_CREDENTIAL_PROXY_GIT_HTTPS` env var are gone.

**`aws_sigv4` (a separate mechanism):** `prepare_credential_proxy_runtime`
also resolves `spec.aws_sigv4` into `runtime.aws_sigv4_rewrites: list[
AwsSigV4RewriteRule]`. Each rule carries either a static
`AwsSigV4Credentials` (access key id + secret key + optional session
token) or a refreshing `credential_provider` — `AwsSigV4CredentialProvider`
for the `assume_role` shape, which mints temporary credentials via STS
using the *parent's own ambient AWS identity* (or a named `profile`, when
set) and re-mints when the cached credential is within a safety margin of
its STS-declared `Expiration` (an explicit, authoritative expiry, unlike
Databricks' opaque OAuth token — so no blind fixed-interval throttle is
needed); or `AwsSigV4ProfileCredentialProvider` for the `profile` shape,
which re-freezes `boto3.Session(profile_name=...).get_credentials()` on
every call instead of tracking its own expiry — boto3 already refreshes a
role-chained or SSO profile internally. Whenever `spec.aws_sigv4`
is non-empty, placeholder `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
`AWS_DEFAULT_REGION` / `AWS_REGION` / `AWS_EC2_METADATA_DISABLED` are
always added to `helper_env_updates` (not opt-in, unlike `inject_env`) —
`boto3` cannot build a request at all without some configured credential.

## Proxy rewrite

`omnigent/inner/egress/proxy.py`: `EgressProxy` takes
`credential_rewrites` and builds two indexes — `_cred_by_host` (the
swap-on-access path) and `_cred_by_synthetic` (the opt-in placeholder
path, populated only for rules carrying a synthetic).
`_rewrite_authorization` (called from both `_forward_https` and
`_handle_http`, the same call sites as the egress allow-list check):

- If the inner request carries an `oa_cred_*` placeholder (across
  `Basic` / `Bearer` / `token`), verify it is bound to this request's
  host (else 403) and re-emit the configured scheme with the real
  secret.
- Else if a rule binds this host and the request carries **no**
  `Authorization` header, inject `Authorization: <scheme> <real>`
  (swap-on-access).
- Else (a foreign non-placeholder header, or no bound rule) forward
  unchanged.

Header parsing/serialization goes through the stdlib email parser
(`BytesParser` + `policy.HTTP`, the same machinery `http.client` uses)
rather than a hand-rolled `split(b"\r\n")` loop — it handles
case-insensitive field names, whitespace, folding, and repeated headers,
and `policy.HTTP` round-trips with CRLF line endings and no folding so
the forwarded request stays byte-faithful. The same helper backs
`_force_connection_close` (dropping hop-by-hop headers) and the
`Authorization` rewrite. When nothing matches, the original client bytes
are forwarded untouched (no needless re-serialization).

`omnigent/inner/egress/controller.py` threads `credential_rewrites`
through `start_egress_proxy`, and keeps `GIT_SSL_CAINFO` in the CA env
keys so `git`/libcurl trusts the MITM CA when it connects to the bound
host (the CA trust is what lets the proxy terminate TLS and inject the
header — it is independent of how the credential is supplied).

**`aws_sigv4` resigning (a separate, parallel path):** `EgressProxy` also
takes `aws_sigv4_rewrites` and builds a third index, `_sigv4_by_host`
(disjoint from `_cred_by_host` — the parser rejects a host bound by more
than one credential-proxy type). `_resign_aws_sigv4` (called as a second,
chained call right after `_rewrite_authorization` at both `_forward_https`
and `_handle_http`, since only one of the two ever does real work per
request):

- Forwards unchanged on the loopback/diagnostic verbs
  (`_CREDENTIAL_INJECTION_FORBIDDEN_METHODS`), same as the header-swap path.
- Forwards unchanged if the bound host's request carries no
  `Authorization`, or one that isn't SigV4-shaped (`is_sigv4_authorization`)
  — defense-in-depth; unlike swap-on-access, `aws_sigv4` never *adds* a
  missing header, only replaces an existing SigV4 one.
- Otherwise calls `omnigent.inner.egress.aws_sigv4.resign_request` — which
  discards `Authorization` / `X-Amz-Date` / `Date` / `X-Amz-Security-Token`
  unconditionally, rebuilds them via `botocore.auth.S3SigV4Auth` (with one
  override — see the module docstring — so `X-Amz-Content-Sha256` is never
  recomputed from the forwarded body, since that value depends only on
  body content and is already correct however it was set), and leaves
  every other header (and the body, in whatever framing) untouched. Raises
  `UnsupportedAwsSigV4RequestError` (→ 403) only for presigned
  query-string auth.

`botocore` is a lazy import inside `resign_request`, gated behind the `s3`
extra (`pip install omnigent[s3]`) — importing `aws_sigv4.py` (and thus
`proxy.py`) never requires it unless an `aws_sigv4` binding is actually
configured and hit.

## Wiring

- `omnigent/inner/os_env.py` — `_start_locked` builds a scoped parent
  env, calls `prepare_credential_proxy_runtime`, merges
  `helper_env_updates` into the helper env (only non-empty for the
  opt-in `env` shim), and passes `rewrites` to the egress proxy. No
  config-pipe credential payload and no helper-side install step.
- `omnigent/inner/sandbox.py` — `credential_proxy` field on
  `SandboxPolicy`, preserved across `_clone_policy_with`. It is
  deliberately **not** part of `to_jsonable`/`from_jsonable`: it's
  parent-side only, and serializing it would risk leaking resolved
  secrets into dumps. The child receives only the optional `oa_cred_*`
  placeholders in its env (swap-on-access entries send nothing); the
  proxy receives only the rewrite table.
- The `resolve` methods of `bwrap_sandbox.py`, `seatbelt_sandbox.py`,
  and `landlock_sandbox.py` propagate
  `credential_proxy=sandbox_spec.credential_proxy`.
- `aws_sigv4_rewrites` follows the identical path as `rewrites` end to
  end: `_start_locked` → `_start_egress_proxy_locked` → `start_egress_proxy`
  → `EgressProxy(aws_sigv4_rewrites=...)`. No `MaterializedFile`/config-file
  step — `boto3` reads credentials from env directly.

## Tests

- `tests/inner/test_credential_proxy.py` — source resolution
  (env/file/command), the swap-on-access default (nothing injected, no
  synthetic minted), opt-in synthetic minting, and the `gh_basic` shape
  (git host swap-on-access, api host env injection); `aws_sigv4` static
  and `assume_role` credential resolution, placeholder-env behavior, and
  the refreshing STS provider's cache/re-mint-near-expiry behavior.
- `tests/inner/egress/test_proxy.py` — swap-on-access injection on a
  bare request, synthetic→real swap for basic/bearer/token against a
  real capturing upstream, the wrong-host 403 leak guard, and
  non-synthetic pass-through; `aws_sigv4` resigning of both a non-chunked
  and the default chunked-with-trailer-checksum `PutObject` shape,
  verified by independently recomputing the expected signature over the
  captured upstream bytes, plus the presigned-query-auth rejection and
  non-SigV4/forbidden-method passthroughs.
- `tests/inner/egress/test_aws_sigv4.py` — pure signer correctness: an
  exact reproduction of AWS's published SigV4 test vector, and the
  header-preservation/stripping rules `resign_request` enforces.
- `tests/spec/test_parser.py` — round-trip + fail-loud for all four
  original types, plus `env`-optional (swap-on-access) parsing; `aws_sigv4`
  round-trip for both credential shapes, fail-loud cases, the
  macOS-allowed contrast with the Go-CLI types, and cross-type
  duplicate-host rejection.
- `tests/inner/sandbox/test_egress_e2e.py` — real-sandbox e2e:
  swap-on-access injects Basic auth on a bare request while the sandbox
  holds neither the secret nor a placeholder; `https_bearer` with `env`
  performs the full env-injection → proxy swap → upstream-sees-real-token
  path; `aws_sigv4` runs a real, unmodified `boto3.client("s3").
  put_object(...)` call inside the sandbox (exercising the default
  chunked+trailer shape) and verifies the upstream received a request
  genuinely signed with the real credential while the sandbox env held
  only the placeholder.

## Non-goals

- **SSH.** This phase covers HTTP(S) Bearer/Basic only. SSH-based git
  remotes are out of scope.
- **`aws_sigv4` presigned query-string auth and non-S3 SigV4 services
  beyond what `service:` already allows.** Header-based signing for any
  SigV4 service is supported (the `service` field is free-form), but this
  phase has only been exercised against S3.
