# Omnigent on Daytona

[Daytona](https://www.daytona.io) sandboxes give you disposable cloud
machines for running Omnigent hosts, two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a
  sandbox from your terminal, ships your local checkout into it, and
  registers it as a host with your server.
- **Server-managed**: the server provisions a sandbox automatically
  when a session is created with `"host_type": "managed"` and
  terminates it when the session is deleted.

Sandboxes boot from the official prebaked host image, so startup is
seconds once Daytona has cached the image as an internal snapshot —
the very first launch from a given image takes a few minutes while
Daytona pulls and snapshots it.

> This directory also contains the source of the **free-tier egress
> relay** (`wrangler.toml`, `src/index.js`) — a Cloudflare Worker that
> lets Daytona Tier 1/2 sandboxes reach your server through Daytona's
> egress firewall. See
> [Free-tier relay setup](#free-tier-relay-setup-tier-12). It is NOT
> a server deploy target.

## Prerequisites

```bash
pip install 'omnigent[daytona]'   # installs the daytona SDK extra
```

> [!IMPORTANT]
> **Egress on Daytona is allowlisted, which shapes how you run hosts
> (CLI-launched and managed alike).** Daytona
> [Tier 1/2 organizations](https://www.daytona.io/docs/en/limits/)
> permit outbound traffic only to a
> [fixed allowlist](https://www.daytona.io/docs/en/network-limits) of
> public domains (git hosts, package managers, the major AI provider
> APIs) that org admins **cannot modify**. Two consequences:
>
> 1. The in-sandbox host's dial-back to your Omnigent `server_url` is
>    blocked unless that URL is on the allowlist — otherwise the
>    launch times out with "managed host did not come online".
> 2. The agent's LLM calls only work against an **allowlisted model
>    endpoint** (`api.openai.com`, `api.anthropic.com`, …). A private
>    or gateway endpoint is blocked the same way.
>
> **Two ways to satisfy this:**
>
> - **Tier 3+** (a $500 *usage top-up* — prepaid sandbox credit, not a
>   fee) lifts the egress restriction entirely: point `server_url` at
>   your real server and use any model endpoint, no relay. Best for
>   teams already on Daytona; cleanest security posture (end-to-end
>   TLS, no middlebox).
> - **Free tier (Tier 1/2) via an allowlisted relay** — `*.workers.dev`
>   passes the firewall, so a tiny Cloudflare Worker that reverse-
>   proxies to your server lets the dial-back through; route any
>   non-allowlisted model endpoint through a second Worker the same
>   way. **Verified working end-to-end on Tier 1.** This inserts a
>   TLS-terminating middlebox, so read
>   [Security considerations](#security-considerations) first. See
>   [Free-tier relay setup](#free-tier-relay-setup-tier-12) below.
>
> If you're evaluating cloud sandboxes from scratch and don't want to
> run a relay, [Modal](../modal/README.md#sandboxes-for-runner-hosts)
> has full egress on its entry tier.

Create an API key in the [Daytona dashboard](https://app.daytona.io)
(Dashboard → Keys) and make it available where the launcher runs —
your shell for the CLI flow, the **server** process for managed
sandboxes:

```bash
export DAYTONA_API_KEY=dtn_…
# Optional: a non-default API endpoint or target region
# export DAYTONA_API_URL=https://app.daytona.io/api
# export DAYTONA_TARGET=us
```

## CLI-launched sandboxes

Provision a sandbox and ship your local checkout into it:

```bash
omnigent sandbox create --provider daytona
```

This pulls the host image, builds wheels from your local checkout, and
overlays them on top — so the sandbox runs *your* code, not whatever
the image was built from. Then register it as a host with your server:

```bash
omnigent sandbox connect --provider daytona \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox (over a PTY session)
and holds the connection open in your terminal — Ctrl-C tears it down.
New sessions targeting that host now run in the sandbox.

Running multiple sandboxes against one server? Pass a unique
`--host-name <label>` to each `connect` — the server keys hosts on
(owner, name), and sandboxes that share a hostname collide.

Sandboxes are disposable. When your code changes, create a new one —
and delete the old one (Daytona sandboxes have no lifetime cap, and
the CLI flow disables idle auto-stop, so abandoned sandboxes keep
billing until removed via the
[dashboard](https://app.daytona.io) or `daytona sandbox delete`).

> [!NOTE]
> On free-tier (Tier 1/2) organizations the `--server` URL must pass
> the egress allowlist or the in-sandbox `omnigent host` can't dial
> back — see the tier note above and the
> [relay setup](#free-tier-relay-setup-tier-12).

To inject LLM/git credentials into a CLI-launched sandbox, set
`OMNIGENT_DAYTONA_SANDBOX_ENV` in your shell to a comma-separated list
of variable names (e.g. `ANTHROPIC_API_KEY,GIT_TOKEN`) before running
`create` — the named variables are copied from your environment into
the sandbox at provision time.

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c
config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: daytona
  server_url: https://your-host    # public URL sandboxes dial back to
```

`server_url` must be reachable *from Daytona's cloud* — a public HTTPS
URL, not `localhost`. Sessions created with `host_type: "managed"`
(the API call or the Web UI's New Sandbox option) then run on a fresh
Daytona sandbox; the create returns immediately and provisioning
happens in the background, exactly like the [Modal managed
flow](../modal/README.md#server-managed-sandboxes) — including
repository workspaces, the first-message rendezvous, and dead-sandbox
relaunch.

Optional `daytona:` settings:

```yaml
sandbox:
  provider: daytona
  server_url: https://your-host
  daytona:
    image: docker.io/<you>/omnigent-host:latest  # default: official image
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]
```

A top-level `sandbox.host_config:` (provider-agnostic) holds verbatim
in-sandbox `~/.omnigent/config.yaml` content — e.g. a `providers:`
block routing a harness through a self-hosted gateway — installed into
the sandbox before `omnigent host` starts. The block is server-managed:
entries injected by a previous launch are replaced or removed on the
next launch/resume, while config created inside the sandbox survives.
Keep secrets out via
`api_key_ref: env:VAR` (resolved in the sandbox against the injected
env). See the [sandbox-runners config
table](../kubernetes/overlays/sandbox-runners/README.md#configuration-sandbox-configyaml)
for the shape.

## Credentials for the sandbox (LLM keys, git tokens)

Daytona has no provider-side named-secret store to attach at sandbox
creation, so credentials are injected as environment variables instead:
`sandbox.daytona.env` lists the **names** of variables to copy from the
**server's own environment** into every sandbox at provision time.
Values never live in the config file — set them where the server runs:

```bash
export OPENAI_API_KEY=sk-…       # on the server
export GIT_TOKEN=github_pat_…    # private-repo clone/fetch/push
```

```yaml
sandbox:
  provider: daytona
  server_url: https://your-host
  daytona:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

A listed name that is **not** set in the server's environment fails the
launch loudly (it would otherwise surface much later as an opaque
harness auth failure inside the sandbox).

Which variables to inject — providers, gateways, subscriptions, git —
is identical to Modal; see the [variable table and per-plan
recipes](../modal/README.md#llm-credentials-for-managed-sandboxes) and
[git credentials](../modal/README.md#git-credentials-private-repositories).
The in-sandbox host forwards the same standard set to its runners, and
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` (as an injected variable) names any
extras.

The same env-injection also carries **credentials for connecting to
the server itself**, for a host that authenticates its dial-back with
user credentials instead of a launch token. Managed launches never
need this: the server injects a per-launch host token automatically.
But a [CLI-launched](#cli-launched-sandboxes) host does when the
server requires authentication — inject the keys for the relevant
server, e.g. `DATABRICKS_HOST` + `DATABRICKS_TOKEN` (or
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`) for a
Databricks-fronted server, by naming them in
`OMNIGENT_DAYTONA_SANDBOX_ENV` before `create` — and the in-sandbox
host mints fresh bearer tokens from them on every reconnect. See
[Connecting to an authenticated
server](../modal/README.md#connecting-to-an-authenticated-server) in
the Modal guide.

> [!NOTE]
> On the **free tier**, the agent's model endpoint must also be
> allowlisted (`api.openai.com`, `api.anthropic.com`, …). A private or
> gateway endpoint is firewalled — route it through a second relay (see
> below) and inject the relay's `*.workers.dev` URL as `OPENAI_BASE_URL`
> / `ANTHROPIC_BASE_URL`.

## Free-tier relay setup (Tier 1/2)

Daytona free-tier (Tier 1/2) sandboxes can only reach an
[allowlisted set of domains](https://www.daytona.io/docs/en/network-limits);
`*.workers.dev` is on it. The ready-to-deploy Cloudflare Worker in this
directory lives there and transparently reverse-proxies every request —
plain HTTP and WebSocket upgrades — to your real Omnigent server, so a
managed host's dial-back (the host tunnel WS, the runner tunnel WS, and
plain HTTP) reaches the server through the firewall.

```bash
npm i -g wrangler          # or use npx
wrangler login             # one-time, free, no credit card
cd deploy/daytona
wrangler deploy --var UPSTREAM_URL:https://your-omnigent-server
# → https://omnigent-daytona-relay.<your-subdomain>.workers.dev
```

Point `sandbox.daytona.server_url` at the printed `*.workers.dev` URL.
For a non-allowlisted model endpoint, deploy a second copy
(`name = "omnigent-llm-relay"`, `UPSTREAM_URL` = your gateway) and
inject its URL as `OPENAI_BASE_URL` via `sandbox.daytona.env`.

**This path is verified end-to-end on a real Daytona Tier 1 org**
(managed create → host dial-back through the relay → runner → real LLM
turn → teardown). Read the security trade-off below before relying on
it.

## Security considerations

- **Injected credentials live in Daytona's control plane.** Daytona has
  no named-secret store, so `sandbox.daytona.env` values are sent to
  Daytona's API as literal sandbox env vars and stored in sandbox
  metadata — a third party now holds whatever you inject (LLM keys,
  `GIT_TOKEN`). Prefer **scoped, short-lived** credentials: a
  fine-grained PAT limited to the repos a session needs, a gateway
  token over a root provider key. (Modal's launcher attaches named
  Modal secrets instead, so its values stay in Modal's secret store —
  a stronger posture; this is the main security difference between the
  two providers.)
- **All managed sandboxes share one Daytona org + API key.**
  Cross-user isolation between Omnigent users rides entirely on
  Daytona's sandbox boundaries, and the shared org key can enumerate
  and delete any user's sandbox. Same single-tenant-org shape as the
  Modal provider; scope the org to this workload and nothing else.
- **The launch token's lifetime is 7 days.** Daytona sandboxes have no
  platform lifetime cap, so the per-launch host token must outlive a
  long-running sandbox across tunnel reconnects — a longer window than
  Modal's ~25h. A leaked token is replayable against the server for
  that window; a relaunch mints a fresh one. Deployments injecting
  their own launcher can set a shorter `token_ttl_s` on
  `ManagedSandboxConfig` if their sandboxes are short-lived.
- **The Tier 1/2 relay workaround is a TLS-terminating MITM.** A relay
  on an allowlisted wildcard domain (`*.vercel.app` / `*.workers.dev`)
  must be an L7 service — it terminates TLS and re-originates, so it
  sees the host launch token and all tunnel payload (runner frames,
  tool output, file contents) in plaintext at its edge. Only use a
  relay you fully control, with logging off; never a shared/public
  one. The direct-egress (Tier 3) path keeps the tunnel end-to-end TLS
  with no middlebox and is the right choice for any
  security-sensitive deployment.

## Troubleshooting

- **"managed host did not come online within 120s"** — on Tier 1/2
  organizations this is almost always the egress firewall blocking the
  host's dial-back to `server_url` (see the tier note above). Verify
  with `curl <server_url>/health` inside a sandbox. On Tier 3+, check
  `/tmp/omnigent-host.log` inside the sandbox.
- **Slow first launch** — the initial create from a new image builds a
  Daytona snapshot (minutes); subsequent launches are seconds.
- **"Organization is suspended: Please verify your email address"** —
  complete email verification in the
  [dashboard](https://app.daytona.io/dashboard/limits) (signing up via
  GitHub/Google SSO arrives pre-verified).

## Lifecycle notes

- **No platform lifetime cap.** Unlike Modal's 24-hour limit, Daytona
  sandboxes run until deleted. Omnigent disables Daytona's 15-minute
  idle auto-stop at provision time (a session host must survive gaps
  between turns); the sandbox is deleted when its session is deleted,
  and the dead-sandbox relaunch path replaces one that crashed or was
  deleted out-of-band.
- **First launch per image is slow.** Daytona builds an internal
  snapshot from the image on first use (minutes for the ~1.4 GiB host
  image); subsequent launches reuse it (seconds).
- **Custom images** work like Modal's: build the `host` target of
  [`deploy/docker/Dockerfile`](../docker/Dockerfile)
  (`--platform linux/amd64`) and push it to any registry Daytona can
  pull from, then set `sandbox.daytona.image` or
  `OMNIGENT_DAYTONA_HOST_IMAGE`.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `DAYTONA_API_KEY` | CLI machine / server | Daytona API credentials (required) |
| `DAYTONA_API_URL` | CLI machine / server | Non-default Daytona API endpoint |
| `DAYTONA_TARGET` | CLI machine / server | Target region for new sandboxes |
| `OMNIGENT_DAYTONA_HOST_IMAGE` | CLI machine / server | Override the host image ref (`sandbox.daytona.image` takes precedence) |
| `OMNIGENT_DAYTONA_SANDBOX_ENV` | CLI machine / server | Comma-separated launcher-side env var names to inject (`sandbox.daytona.env` takes precedence for managed) |
