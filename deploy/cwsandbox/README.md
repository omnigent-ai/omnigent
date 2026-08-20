# CoreWeave Sandbox provider

[CoreWeave Sandbox](https://docs.coreweave.com/products/sandboxes) gives you
disposable cloud machines for running Omnigent hosts, two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a sandbox
  from your terminal, ships your local checkout into it, and registers it as a
  host with your server.
- **Server-managed**: the server provisions a sandbox automatically when a
  session is created with `"host_type": "managed"` and terminates it when the
  session is deleted.

The launcher wraps the official
[`cwsandbox`](https://github.com/coreweave/cwsandbox-client) Python SDK, gated
behind the `cwsandbox` extra and imported lazily — same posture as the Modal and
Daytona launchers. Sandboxes boot from the official prebaked host image, so
startup is seconds.

Two traits shape the rest of this guide:

- **No local port forward.** CoreWeave Sandbox can't forward a sandbox→laptop
  callback port, so the interactive in-sandbox `omnigent login` / App OAuth step
  is skipped automatically (as on Modal and Daytona) — fine for token/OIDC-auth
  servers.
- **No egress by default.** CW Sandbox blocks outbound traffic unless asked; the
  launcher requests `egress_mode: internet` so the host can reach your server and
  the agent can reach its model endpoint.

```bash
pip install 'omnigent[cwsandbox]'
```

## Prerequisites

Create a CoreWeave Sandbox API key and make it available where the launcher
runs — your shell for the CLI flow, the **server** process for managed sandboxes
(12-factor; never in config files):

```bash
export CWSANDBOX_API_KEY=...                          # CoreWeave Sandbox API key
export CWSANDBOX_BASE_URL=https://api.cwsandbox.com   # optional (this is the default)
```

## The host image

Sandboxes boot from `ghcr.io/omnigent-ai/omnigent-host:latest`, published by CI
from the `host` target of [`deploy/docker/Dockerfile`](../docker/Dockerfile)
with Omnigent and its dependencies preinstalled — including the coding-harness
CLIs (`claude`, `codex`, `pi`, `kiro-cli`), so agents on any harness run without an
in-sandbox install.

To use a different image (a fork, or extra tooling baked in), build the same
target and push it anywhere CoreWeave can pull from:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  --platform linux/amd64 \
  -t docker.io/<you>/omnigent-host:latest .
docker push docker.io/<you>/omnigent-host:latest
```

Then point Omnigent at it — `OMNIGENT_CWSANDBOX_HOST_IMAGE` for the CLI flow, or
`sandbox.cwsandbox.image` in the server config for the managed flow.

> [!NOTE]
> Building on Apple Silicon? Pass `--platform linux/amd64` — sandboxes run
> x86_64.

## CLI-launched sandboxes

Provision a sandbox and ship your local checkout into it:

```bash
omnigent sandbox create --provider cwsandbox --server https://your-host
```

This pulls the host image, builds wheels from your local checkout, and overlays
them on top — so the sandbox runs *your* code, not whatever the image was built
from. Then register it as a host with your server:

```bash
omnigent sandbox connect --provider cwsandbox \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the connection open
in your terminal — Ctrl-C tears it down. New sessions targeting that host now run
in the sandbox.

Running multiple sandboxes against one server? Pass a unique `--host-name
<label>` to each `connect` — the server keys hosts on (owner, name), and
sandboxes that share a hostname collide.

Sandboxes are disposable. When your code changes, create a new one.

To inject LLM/git credentials into a CLI-launched sandbox, set
`OMNIGENT_CWSANDBOX_SANDBOX_ENV` in your shell to a comma-separated list of
variable names (e.g. `ANTHROPIC_API_KEY,GIT_TOKEN`) before running `create` — the
named variables are copied from your environment into the sandbox at provision
time. A listed name that is **not** set fails the launch loudly (it would
otherwise surface much later as an opaque harness auth failure inside the
sandbox).

### Connecting to an authenticated server

`connect` runs `omnigent host` inside the sandbox, and that host must present
credentials when it dials back to a server that requires authentication. The
interactive `omnigent login` browser flow can't run inside a sandbox (no callback
port forward), so inject the keys for the relevant server instead — name them in
`OMNIGENT_CWSANDBOX_SANDBOX_ENV` before `create`:

```bash
export OMNIGENT_CWSANDBOX_SANDBOX_ENV=DATABRICKS_HOST,DATABRICKS_TOKEN
omnigent sandbox create --provider cwsandbox --server https://your-host
```

The in-sandbox host mints a fresh bearer token from those credentials on every
connect and reconnect. For a Databricks-fronted server, inject `DATABRICKS_HOST`
plus either `DATABRICKS_TOKEN` (a PAT) or `DATABRICKS_CLIENT_ID` /
`DATABRICKS_CLIENT_SECRET` (an OAuth service principal — re-minting keeps a
long-lived sandbox connected past any single token's expiry).

A server with no authentication on the host tunnel needs none of this, and
neither do [server-managed sandboxes](#server-managed-sandboxes) — those
authenticate with a server-minted per-launch token automatically.

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c config.yaml`,
or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host    # public URL sandboxes dial back to
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

`provider` + `server_url` is a complete config. `server_url` **must be reachable
from CoreWeave** — the host inside the sandbox opens an outbound WebSocket to it,
not `localhost`. For local testing, expose your server with a tunnel
(`cloudflared` / `ngrok`) and point `server_url` at the tunnel URL. The server
itself needs `CWSANDBOX_API_KEY` (and optional `CWSANDBOX_BASE_URL`) in its
environment.

Sessions created with `host_type: "managed"` (the API call or the Web UI's New
Sandbox option) then run on a fresh CW sandbox; the create returns immediately
and provisioning happens in the background, exactly like the [Modal managed
flow](../modal/README.md#server-managed-sandboxes) — including repository
workspaces, the first-message rendezvous, and dead-sandbox relaunch.

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

Each managed sandbox authenticates back with a server-minted, per-launch token;
no user credentials enter the sandbox for the server connection.

Optional `cwsandbox:` settings:

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host
  cwsandbox:
    image: docker.io/<you>/omnigent-host:latest        # default: official image
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]  # server env var NAMES to inject
```

### Managed hosts and server auth

How the dial-back authenticates depends on how the **server** does auth, and
there is one interaction worth knowing before you deploy. A managed sandbox opens
two kinds of connections back to the server: the **host tunnel**, which the
per-launch token authenticates directly (always works), and one **runner tunnel**
per session, opened by the runner subprocess — which authenticates with whatever
server credential it can resolve, **not** the per-launch host token.

The consequence:

- **Header / OIDC-proxy auth, or single-user (no-auth) servers** — the runner
  tunnel needs no extra identity, so managed hosts work out of the box.
- **The built-in `accounts` provider (`OMNIGENT_AUTH_ENABLED=1`)** — the runner
  tunnel additionally requires a *user* identity, which the per-launch host token
  does not carry, so the runner dial-back is refused (`403`) even though the host
  tunnel connects. This is a framework-level managed-host interaction shared by
  **all** sandbox providers (Modal / Daytona / Islo / cwsandbox), not specific to
  cwsandbox.

So for a managed cwsandbox deployment, front the server with **header or OIDC
auth** (a reverse proxy / IdP injects the user identity on every request,
including the runner WebSocket — see
[`deploy/README.md#auth`](../README.md#auth)), or run it single-user. The
`accounts` provider is fine for CLI-launched hosts (you `omnigent login`, and
that token is what the in-sandbox host forwards), but not yet for the managed
runner dial-back.

## Model credentials (LLM keys)

A fresh sandbox has no model credentials. Name the variables to inject in
`OMNIGENT_CWSANDBOX_SANDBOX_ENV` (CLI) or `sandbox.cwsandbox.env` (managed); the
launcher copies the value from the launching environment into the sandbox, and
the in-sandbox host forwards the standard harness credential vars to its runners:

```bash
export ANTHROPIC_API_KEY=sk-ant-…   # on the server (managed) or in your shell (CLI)
```

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host
  cwsandbox:
    env: [ANTHROPIC_API_KEY]
```

Which variables to inject — providers, gateways, subscriptions, git — is
identical to Modal; see the [variable table and per-plan
recipes](../modal/README.md#llm-credentials-for-managed-sandboxes) and [git
credentials](../modal/README.md#git-credentials-private-repositories). For a
Claude **subscription** specifically, run `claude setup-token` on your own
machine (one-time browser auth) and inject the resulting long-lived token as
`CLAUDE_CODE_OAUTH_TOKEN`. For env vars beyond the standard set, inject
`OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

## Git credentials (private repositories)

Inject an HTTPS token as `GIT_TOKEN` (GitLab: add `GIT_USERNAME=oauth2`) via
`OMNIGENT_CWSANDBOX_SANDBOX_ENV` / `sandbox.cwsandbox.env`. The host image's git
credential helper answers HTTPS auth from it for both the launch-time clone and
the agent's later `fetch` / `push`, writing nothing to disk. Use HTTPS repository
URLs. Details by provider match the [Modal git
guide](../modal/README.md#git-credentials-private-repositories).

## Security considerations

- **Injected credentials live in CoreWeave's control plane.** The launcher passes
  `sandbox.cwsandbox.env` values to the CoreWeave API as sandbox environment
  variables, so a third party holds whatever you inject (LLM keys, `GIT_TOKEN`)
  for the sandbox's life. Prefer **scoped, short-lived** credentials: a
  fine-grained PAT limited to the repos a session needs, a gateway token over a
  root provider key.
- **All managed sandboxes share one CoreWeave org + `CWSANDBOX_API_KEY`.**
  Cross-user isolation rides on CoreWeave's sandbox boundaries, and the shared key
  can enumerate and delete any sandbox — the same single-tenant-org shape as the
  Modal and Daytona providers. Scope the org to this workload and nothing else.
- **The launch token's lifetime tracks the sandbox lifetime.** CW Sandbox's
  lifetime is operator-overridable (`OMNIGENT_CWSANDBOX_MAX_LIFETIME_S`, 24h
  default), so the per-launch host token TTL is derived from it — always above the
  cap, so a live sandbox can re-authenticate across reconnects while a leaked
  token can't outlive the sandbox it came from. A relaunch mints a fresh one.

## Notes / limits

- Sandboxes are reaped at `max_lifetime_seconds` (24h default; override with
  `OMNIGENT_CWSANDBOX_MAX_LIFETIME_S`). The managed launch-token TTL is set above
  that so reconnects keep working.
- Egress defaults to none on CW Sandbox; the launcher requests `egress_mode:
  internet` so the host can reach the server and the agent can reach its model
  endpoint.

## Troubleshooting

- **"managed host did not come online within 120s"** — the server waits up to two
  minutes for the in-sandbox host to register. If it times out, check that
  `server_url` is publicly reachable from CoreWeave, then inspect the in-sandbox
  host log: `/tmp/omnigent-host.log`.
- **Slow first launch** — the first launch from a given image waits on a cold
  registry pull before the sandbox is ready; subsequent launches reuse the cached
  image and start in seconds.
- **Agent has no credentials** — verify the injected var names match the
  forwarded set (or are named in `OMNIGENT_RUNNER_ENV_PASSTHROUGH`), and that each
  name was actually set in the launching environment.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `CWSANDBOX_API_KEY` | CLI machine / server | CoreWeave Sandbox API credentials (required) |
| `CWSANDBOX_BASE_URL` | CLI machine / server | Non-default CW Sandbox API endpoint (default `https://api.cwsandbox.com`) |
| `OMNIGENT_CWSANDBOX_HOST_IMAGE` | CLI machine / server | Override the host image ref (`sandbox.cwsandbox.image` takes precedence for managed) |
| `OMNIGENT_CWSANDBOX_SANDBOX_ENV` | CLI machine / server | Comma-separated launcher-side env var names to inject (`sandbox.cwsandbox.env` takes precedence for managed) |
| `OMNIGENT_CWSANDBOX_MAX_LIFETIME_S` | CLI machine / server | Sandbox lifetime cap in seconds (default 24h); also derives the managed launch-token TTL |
| `OMNIGENT_RUNNER_ENV_PASSTHROUGH` | inside the sandbox (injected) | Extra env var names the host forwards to runners |
| `GIT_TOKEN` / `GIT_USERNAME` | inside the sandbox (injected) | HTTPS credentials for private repository clone / fetch / push |

## Smoke test

Validate the API primitives directly (no Omnigent or SDK install needed — stdlib
+ curl only):

```bash
export CWSANDBOX_API_KEY=...
python tests/e2e/integrations/deploy/cwsandbox/smoke_test.py
```
