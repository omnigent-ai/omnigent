# Secretless Credential Proxy

> **IMPLEMENTED.** Config surface: `os_env.sandbox.credential_proxy`.
> Code: `omnigent/inner/credential_proxy.py`,
> `omnigent/inner/egress/proxy.py`, `omnigent/spec/parser.py`.

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

## Tests

- `tests/inner/test_credential_proxy.py` — source resolution
  (env/file/command), the swap-on-access default (nothing injected, no
  synthetic minted), opt-in synthetic minting, and the `gh_basic` shape
  (git host swap-on-access, api host env injection).
- `tests/inner/egress/test_proxy.py` — swap-on-access injection on a
  bare request, synthetic→real swap for basic/bearer/token against a
  real capturing upstream, the wrong-host 403 leak guard, and
  non-synthetic pass-through.
- `tests/spec/test_parser.py` — round-trip + fail-loud for all four
  types, plus `env`-optional (swap-on-access) parsing.
- `tests/inner/sandbox/test_egress_e2e.py` — real-sandbox e2e:
  swap-on-access injects Basic auth on a bare request while the sandbox
  holds neither the secret nor a placeholder; `https_bearer` with `env`
  performs the full env-injection → proxy swap → upstream-sees-real-token
  path.

## Non-goals

- **SSH.** This phase covers HTTP(S) Bearer/Basic only. SSH-based git
  remotes are out of scope.
