# Omnigent on Modal

[Modal](https://modal.com) plays two distinct roles for Omnigent:

1. **[Server deploy target](#deploying-the-server)** — run the
   Omnigent server itself on Modal as a single always-on web server
   (`modal_app.py` in this directory).
2. **[Sandbox provider](#sandboxes-for-runner-hosts)** — disposable
   cloud machines for running Omnigent *hosts*, so sessions execute in
   the cloud instead of on your laptop.

The two are independent: you can deploy the server anywhere and still
use Modal sandboxes for hosts, or vice versa.

## Deploying the server

Run the Omnigent server on Modal as a single always-on web server.
`modal_app.py` pulls the standard server image and launches the same
Docker entrypoint every other platform uses; Modal provides the HTTPS
URL, log streaming, and a persistent Volume for the artifact store —
uploaded agent bundles survive restarts and redeploys here, unlike on
Heroku or Cloudflare.

### Prerequisites

- A Modal account and the CLI: `pip install modal && modal setup`.
  No Docker needed locally — Modal's builders pull the image.
- A Postgres database. Modal has no managed Postgres — the fastest is
  **Neon**: create one at [pg.new](https://pg.new) and copy the
  connection string.

### Deploy

```bash
# 1. One secret bundle with the three required values. The app URL is
#    deterministic: https://<workspace>--omnigent-server.modal.run
#    (your workspace name is shown by `modal profile current`).
modal secret create omnigent-deploy \
  DATABASE_URL='postgres://…neon.tech/…' \
  OMNIGENT_ACCOUNTS_COOKIE_SECRET="$(openssl rand -hex 32)" \
  OMNIGENT_ACCOUNTS_BASE_URL='https://<workspace>--omnigent-server.modal.run'

# 2. Ship it.
modal deploy deploy/modal/modal_app.py
```

`modal deploy` prints the live URL — if it differs from what you guessed
in step 1 (e.g. a non-default Modal environment adds a suffix), update
the secret and redeploy.

The first boot runs DB migrations over the network (~1 minute on Neon).

**Get the admin password:** the first boot prints it to the app log:

```bash
modal app logs omnigent
```

```
✓ Created initial admin account (accounts auth provider).
    password: <generated>
```

Log in as the admin and invite teammates from **Members** in the web UI.

> To set a known admin password instead, add
> `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD=<password>` to the
> `omnigent-deploy` secret before the first deploy.

### Modal-specific caveats

- **2 MiB WebSocket message cap.** Modal's ingress limits WebSocket
  messages to 2 MiB each, well below the runner tunnel's own 100 MiB
  allowance. Normal streaming traffic (events, terminal frames) is far
  smaller, but a single very large tool payload over the tunnel can
  fail on this platform.
- **Connections reset at the 24 h input timeout.** A proxied WebSocket
  occupies one Modal function input, and inputs are capped at 24 hours
  — so a tunnel lives at most a day before being cut. Runners
  auto-reconnect (0.5–10 s jittered backoff).
- **One always-on container by design.** `min_containers=1` /
  `max_containers=1` in `modal_app.py`: the runner registry is
  in-memory, so traffic must land on a single container, and
  scale-to-zero would kill live tunnels. Don't raise `max_containers`
  expecting horizontal scaling.
- **No SQLite tier.** The artifact Volume is durable but is not a place
  for a SQLite database (eventual-consistency semantics); use Postgres.

### Use your own IdP instead (OIDC)

Add the OIDC values to the `omnigent-deploy` secret (Modal secrets are
key-value bundles; `modal secret create` with the same name replaces it)
and redeploy:

```bash
modal secret create omnigent-deploy \
  DATABASE_URL='…' \
  OMNIGENT_AUTH_PROVIDER=oidc \
  OMNIGENT_OIDC_ISSUER='https://github.com' \
  OMNIGENT_OIDC_CLIENT_ID='…' \
  OMNIGENT_OIDC_CLIENT_SECRET='…' \
  OMNIGENT_OIDC_REDIRECT_URI='https://<workspace>--omnigent-server.modal.run/auth/callback' \
  OMNIGENT_OIDC_COOKIE_SECRET="$(openssl rand -hex 32)"
```

The IdP registration steps (GitHub / Google / Okta callback URLs, domain
allow-listing) are identical to the other platforms — see
[`deploy/render/README.md`](../render/README.md#use-your-own-idp-instead-oidc).

### Custom domain

Pass `custom_domains=["omnigent.example.com"]` to `@modal.web_server`
in `modal_app.py` (requires a paid Modal plan), point your DNS at Modal
per the printed instructions, and update `OMNIGENT_ACCOUNTS_BASE_URL`
(or the OIDC redirect URI) to match.

### Upgrading

`modal deploy deploy/modal/modal_app.py` again — Modal re-resolves
`ghcr.io/omnigent-ai/omnigent-server:latest`, so a redeploy is an
upgrade. The rollout replaces the container; runners reconnect.

### Cost

Modal bills actual usage: memory at ~$0.008/GiB-hour and CPU by the
cycle (so an idle server's CPU line is small). An always-on 1 GiB
instance runs roughly **$6–8/month**, which fits inside the Starter
plan's **$30/month of free credits** — making this effectively free for
a lightly loaded server. Rates: [modal.com/pricing](https://modal.com/pricing).

## Sandboxes for runner hosts

Modal sandboxes give you disposable cloud machines for running
Omnigent hosts — no laptop tethered to a session, no VM to babysit.
There are two ways to use them:

1. **CLI-launched sandboxes** — you provision a sandbox from your
   terminal and register it as a host with your server. Good for
   development and for running your local checkout's code in the cloud.
2. **Server-managed sandboxes** — the server provisions a sandbox
   automatically when a session is created with
   `"host_type": "managed"`, and terminates it when the session is
   deleted. Good for production deployments where users shouldn't have
   to think about hosts at all.

Both boot from the official prebaked host image, so startup is seconds,
not minutes.

### Sandbox prerequisites

```bash
pip install 'omnigent[modal]'   # installs the modal SDK extra
modal token new                  # one-time browser auth with Modal
```

`modal token new` writes `~/.modal.toml`. Anywhere Omnigent needs to
talk to Modal (your laptop for the CLI flow, the server for the managed
flow), Modal credentials must be available — either that file or the
`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` environment variables.

### The host image

Sandboxes boot from `ghcr.io/omnigent-ai/omnigent-host:latest`, an image
published by CI from the `host` target of
[`deploy/docker/Dockerfile`](../docker/Dockerfile) with Omnigent
and its dependencies preinstalled — including the coding-harness CLIs
(`claude`, `codex`, `pi`, `kiro-cli`), so agents on any harness run without an
in-sandbox install.

To use a different image (a fork, or extra tooling baked in), build the
same target and push it anywhere Modal can pull from:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  -t docker.io/<you>/omnigent-host:latest .
docker push docker.io/<you>/omnigent-host:latest
```

Then point Omnigent at it — `OMNIGENT_MODAL_HOST_IMAGE` for the CLI
flow, or `sandbox.modal.image` in the server config for the managed
flow (see below). For private registries, set
`OMNIGENT_MODAL_REGISTRY_SECRET` to the name of a
[Modal secret](https://modal.com/secrets) containing
`REGISTRY_USERNAME` / `REGISTRY_PASSWORD`.

> [!NOTE]
> Building on Apple Silicon? Pass `--platform linux/amd64` — Modal
> sandboxes run x86_64.

### CLI-launched sandboxes

Provision a sandbox and ship your local checkout into it:

```bash
omnigent sandbox create --provider modal
```

This pulls the host image, builds wheels from your local checkout, and
overlays them on top — so the sandbox runs *your* code, not whatever
the image was built from. Then register it as a host with your server:

```bash
omnigent sandbox connect --provider modal \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the
connection open in your terminal — Ctrl-C tears it down. New sessions
targeting that host now run in the sandbox.

Running multiple sandboxes against one server? Pass a unique
`--host-name <label>` to each `connect` — the server keys hosts on
(owner, name), and sandboxes that share a hostname collide.

Sandboxes are disposable. When your code changes, create a new one.

> [!NOTE]
> Modal caps sandbox lifetime at 24 hours (a platform hard limit).
> Re-run `create` + `connect` to roll the host onto a fresh sandbox.

For provider-side lifecycle (list / status / terminate), use Modal's
own tooling — the [Modal dashboard](https://modal.com/sandboxes) or the
`modal` CLI.

### Connecting to an authenticated server

`connect` runs `omnigent host` inside the sandbox, and that host must
present credentials when it dials back to a server that requires
authentication. The interactive `omnigent login` browser flow can't
run inside a sandbox, so inject the keys for the relevant server
instead: park them in a [Modal secret](https://modal.com/secrets) and
name it in `OMNIGENT_MODAL_SANDBOX_SECRETS` (comma-separated) before
running `create`:

```bash
modal secret create omnigent-server-auth \
  DATABRICKS_HOST=https://example.databricks.com \
  DATABRICKS_TOKEN=<your-pat>
export OMNIGENT_MODAL_SANDBOX_SECRETS=omnigent-server-auth
omnigent sandbox create --provider modal
```

The in-sandbox host mints a fresh bearer token from those credentials
on every connect and reconnect. For a server fronted by Databricks
authentication, inject `DATABRICKS_HOST` plus either
`DATABRICKS_TOKEN` (a PAT) or `DATABRICKS_CLIENT_ID` /
`DATABRICKS_CLIENT_SECRET` (an OAuth service principal — re-minting
keeps a long-lived sandbox connected past any single token's expiry).

A server with no authentication on the host tunnel needs none of this,
and neither do [server-managed sandboxes](#server-managed-sandboxes) —
those authenticate with a server-minted per-launch token automatically.

(The same env var also carries LLM / git credentials for CLI-launched
sandboxes — any secret named in `OMNIGENT_MODAL_SANDBOX_SECRETS` lands
in the sandbox environment, exactly like `sandbox.modal.secrets` does
for managed launches.)

### Server-managed sandboxes

With managed hosts, the server does all of the above per session.
Add a `sandbox:` section to the server config (`omnigent server -c
config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host    # public URL sandboxes dial back to
```

`server_url` must be reachable *from Modal's cloud* — a public HTTPS
URL, not `localhost`. The server itself needs Modal credentials in its
environment (`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`, or a mounted
`~/.modal.toml`).

Now create sessions with `host_type: "managed"`:

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

The create returns immediately; the server provisions a fresh sandbox
in the background, starts a host in it, and binds the session once the
host comes online (`host_id` / `workspace` appear on
`GET /v1/sessions/{id}` when it does). A message posted before then
waits for the launch to settle, so you can send the first prompt right
away. Deleting the session terminates the sandbox and removes the
host. Each sandbox authenticates back with a server-minted, per-launch
token — no user credentials ever enter the sandbox.

Optional `modal:` settings:

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    image: docker.io/<you>/omnigent-host:latest   # default: official image
    secrets: [omnigent-llm]                       # Modal secrets to inject
```

### LLM credentials for managed sandboxes

A fresh sandbox has no API keys. Park your provider credentials in a
[Modal secret](https://modal.com/secrets) and list it under
`sandbox.modal.secrets` — its env vars are injected into every managed
sandbox, and the in-sandbox host forwards the standard harness
credential vars to its runners:

```bash
modal secret create omnigent-llm \
  OMNIGENT_ANTHROPIC_API_KEY=sk-ant-… OPENAI_API_KEY=sk-…
```

The forwarded set covers the variables the harnesses themselves
resolve — and it reaches well beyond the first-party APIs. The
`*_BASE_URL` variables redirect each harness to *any* compatible
endpoint, so the same mechanism covers frontier providers, gateways
like [OpenRouter](https://openrouter.ai) and
[LiteLLM](https://docs.litellm.ai), and self-hosted open-source models:

| Variable | Enables |
|---|---|
| `OMNIGENT_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` | Claude models on the Anthropic API (claude-sdk, pi, claude-code harnesses). Prefer the `OMNIGENT_` form for Claude Code so the raw `ANTHROPIC_API_KEY` env var is not present in the CLI process. |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | Anthropic-compatible gateways — point claude-code at a LiteLLM proxy, a Bedrock/Vertex bridge, or a corporate gateway |
| `CLAUDE_CODE_OAUTH_TOKEN` | claude-code with a Claude subscription (no API key) |
| `OPENAI_API_KEY` | OpenAI models on the OpenAI API (codex, openai-agents harnesses) |
| `OPENAI_BASE_URL` | Any OpenAI-compatible endpoint — the de-facto standard API of the open-model ecosystem. Gateways (OpenRouter, LiteLLM), hosted open-weights providers (Together, Fireworks, Groq), or self-hosted vLLM / Ollama — this is how Llama, Qwen, DeepSeek, and friends plug in |
| `CODEX_ACCESS_TOKEN` | codex with a ChatGPT Business/Enterprise workspace |
| `GEMINI_API_KEY` | Gemini models on the Google AI API |

Common setups:

- **Claude with an API key** — put `OMNIGENT_ANTHROPIC_API_KEY` in the secret.
  Omnigent resolves it into Claude Code's `apiKeyHelper`; do not also set
  `ANTHROPIC_API_KEY` unless you are okay with Claude Code detecting the raw
  custom key env var.
- **Claude with a subscription** — run `claude setup-token` on your own
  machine (one-time browser auth) and store the resulting long-lived
  token as `CLAUDE_CODE_OAUTH_TOKEN`.
- **Codex with an API key** — put `OPENAI_API_KEY` in the secret.
- **Codex with a ChatGPT Business/Enterprise plan** — mint a
  [Codex access token](https://developers.openai.com/codex/enterprise/access-tokens)
  in the ChatGPT admin console (a workspace admin must grant the
  permission) and store it as `CODEX_ACCESS_TOKEN`.
- **Codex with a ChatGPT Plus/Pro plan** — there is no headless token
  for personal plans. Codex stores personal-plan auth in
  `~/.codex/auth.json` with effectively single-use refresh tokens, so
  copies of that file across machines invalidate each other — it can't
  be injected into disposable sandboxes via a shared secret. Use an
  API key or `codex login --device-auth` inside a long-lived sandbox
  instead (device-code login must first be enabled in ChatGPT →
  Settings → Security).
- **Gateways and open-source models** — set `OPENAI_BASE_URL` to the
  endpoint plus its key as `OPENAI_API_KEY` (e.g.
  `OPENAI_BASE_URL=https://openrouter.ai/api/v1` with an OpenRouter
  key, or your own vLLM server's URL). Anthropic-side gateways work
  the same way via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`.

For env vars beyond the standard set, add
`OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2` to the secret — the
host forwards the named extras to its runners.

To check what actually landed in a sandbox, exec into it with Modal's
CLI and inspect the environment:

```bash
modal shell <sandbox-id>          # interactive shell in the sandbox
env | grep -E 'ANTHROPIC|OPENAI|GIT'
```

### Git credentials (private repositories)

Sandboxes clone repository workspaces anonymously by default, which
covers public repositories only. For private repositories — both the
clone the server runs at session create and the `git fetch` / `git
push` the agent runs later — put an HTTPS token in a Modal secret as
`GIT_TOKEN`:

```bash
modal secret create omnigent-git GIT_TOKEN=github_pat_…
```

and list the secret under `sandbox.modal.secrets` (multiple secrets
compose, so keeping git and LLM credentials in separate secrets is
fine):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    secrets: [omnigent-llm, omnigent-git]
```

The host image ships a git credential helper that answers HTTPS
authentication from `GIT_TOKEN`, so nothing is written to disk and no
URL ever embeds the token. Details by provider:

- **GitHub** — use a [fine-grained personal access
  token](https://github.com/settings/personal-access-tokens) scoped to
  the repositories the sandbox needs (Contents: read, or read/write if
  the agent pushes). The default auth username (`x-access-token`)
  is already correct.
- **GitLab** — create a project or personal access token with
  `read_repository` / `write_repository` and add
  `GIT_USERNAME=oauth2` to the secret.
- **Other HTTPS remotes** — any server accepting basic auth works;
  set `GIT_USERNAME` if it requires a specific username.

Use HTTPS repository URLs (`https://github.com/org/repo`) for private
workspaces — SSH URLs (`git@github.com:…`) would need a key and
known-hosts setup inside the sandbox, which the managed flow does not
provide.

The token is forwarded host→runner (like the LLM credentials above),
so the agent's own git commands authenticate the same way the
launch-time clone did. If the agent should also create commits, bake
or configure `user.name` / `user.email` via your agent's instructions
or a custom image.

### Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | CLI machine / server | Modal API credentials (alternative to `~/.modal.toml`) |
| `OMNIGENT_MODAL_HOST_IMAGE` | CLI machine / server | Override the host image ref (`sandbox.modal.image` takes precedence for managed) |
| `OMNIGENT_MODAL_REGISTRY_SECRET` | CLI machine / server | Modal secret name with `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` for private image pulls |
| `OMNIGENT_MODAL_SANDBOX_SECRETS` | CLI machine / server | Comma-separated Modal secret names to inject (`sandbox.modal.secrets` takes precedence for managed) |
| `OMNIGENT_RUNNER_ENV_PASSTHROUGH` | inside the sandbox (set via a Modal secret) | Extra env var names the host forwards to runners |
| `GIT_TOKEN` | inside the sandbox (set via a Modal secret) | HTTPS token for private repository clone / fetch / push |
| `GIT_USERNAME` | inside the sandbox (set via a Modal secret) | Auth username paired with `GIT_TOKEN` (default `x-access-token`; GitLab uses `oauth2`) |

All of the above are supported public configuration. The variables the
managed launcher itself sets inside the sandbox —
`OMNIGENT_HOST_TOKEN`, `OMNIGENT_HOST_ID`, `OMNIGENT_HOST_NAME` —
are internal plumbing (server-minted per launch) and are never set by
users.

### Limits and troubleshooting

- **24-hour lifetime.** Modal hard-caps sandbox lifetime at 24 hours.
  CLI flow: re-run `create` + `connect`. Managed flow: nothing to do —
  when the sandbox dies, the next message to the session provisions a
  fresh one under the same host (the session binding survives; a
  repository workspace is re-cloned). Uncommitted workspace changes
  die with the sandbox, so push work you care about.
- **Resources.** Sandboxes are created with 2 CPUs and 4 GiB of
  memory.
- **Managed launch hangs then fails.** The server waits up to two
  minutes for the in-sandbox host to come online. If it times out,
  check that `server_url` is publicly reachable from Modal, then
  inspect the host log inside the sandbox: `/tmp/omnigent-host.log`.
- **Image pull failures.** Private image without
  `OMNIGENT_MODAL_REGISTRY_SECRET` set, or a secret missing
  `REGISTRY_USERNAME` / `REGISTRY_PASSWORD`.
- **Agent has no credentials.** Verify the Modal secret is listed in
  `sandbox.modal.secrets` and its var names match the forwarded set
  above (or are named in `OMNIGENT_RUNNER_ENV_PASSTHROUGH`).
