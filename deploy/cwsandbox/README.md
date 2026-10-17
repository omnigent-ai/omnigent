# CoreWeave Sandbox provider

[CoreWeave Sandbox](https://docs.coreweave.com/products/sandboxes) gives you
disposable cloud machines for running Omnigent hosts. By default, this integration
uses serverless capacity operated by CoreWeave: no cluster or sandbox runner
setup is required. For your own cluster, see [CKS sandboxes](#cks-sandboxes).
Both placements support two Omnigent workflows:

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
startup depends on image size and registry caching.

Two traits shape the rest of this guide:

- **No local port forward.** CoreWeave Sandbox can't forward a sandbox→laptop
  callback port, so `create` skips the automatic browser-based login step.
  Accounts authentication needs no browser. Use the explicit
  [sandbox login step](#connecting-to-an-authenticated-server) before `connect`.
- **Outbound connections.** The sandbox must reach your Omnigent server, model
  API, and any repository or package hosts your workload uses. Serverless uses
  CoreWeave's network defaults. [CKS networking](#cks-sandboxes) is configured
  by your cluster administrator.

```bash
pip install 'omnigent[cwsandbox]'
```

## Prerequisites

Use Python 3.12 or later and install `omnigent[cwsandbox]` (SDK 1.14.3 or later in
the 1.x series). For CLI launches, run from an Omnigent checkout with
[`uv`](https://docs.astral.sh/uv/) 0.11.8 or later. See
[development setup](../../CONTRIBUTING.md#development-setup). From the checkout root:

```bash
uv --version  # must report 0.11.8 or later
export OMNIGENT_SKIP_WEB_UI=true
uv sync --extra cwsandbox
source .venv/bin/activate
```

`create` invokes `uv` from `PATH` again to build wheels. Ensure the newer `uv`
binary is on `PATH` for both `sync` and `create`. Using `uvx` for just the first
command doesn't update the binary used by `create`.

The exported flag skips the web UI during installation and subsequent wheel
builds. The CLI ships wheels built from the checkout. Installing only the PyPI
wheel is sufficient for a managed server, but not for `omnigent sandbox create`.

### Serverless sandboxes (default)

Sign up for a [W&B account](https://wandb.ai) and get an API key
from [W&B Authorize](https://wandb.ai/authorize). This self-service path doesn't
require a CoreWeave cloud account. Set the key where the launcher runs: your
shell for CLI launches, or the Omnigent server process for managed sessions.

```bash
export WANDB_API_KEY=...
```

The `cwsandbox` extra installs `cwsandbox[wandb]`. By default, the launcher uses
`auth=AuthStrategy.WANDB`, even when `CWSANDBOX_API_KEY` is also set. It connects
to `https://api.cwsandbox.com` and requests serverless placement by default.
No runner ID or user-managed policy is needed.

For example, to check W&B authentication directly with the SDK:

```python
from cwsandbox import AuthStrategy, Sandbox

with Sandbox.run("sleep", "infinity", auth=AuthStrategy.WANDB) as sandbox:
    print(sandbox.exec(["echo", "Hello!"]).result().stdout)
```

See [sandbox credentials](https://docs.coreweave.com/products/sandboxes/get-started#choose-a-credential)
for account requirements, supported credentials, and W&B Dedicated limitations.
Existing CoreWeave customers can select `OMNIGENT_CWSANDBOX_AUTH_STRATEGY=coreweave_api_key`
and set `CWSANDBOX_API_KEY` instead.

### CKS sandboxes

To use your own CoreWeave Kubernetes Service (CKS) cluster, first
[deploy a sandbox runner and configure its policy](https://docs.coreweave.com/products/sandboxes/get-started#deploy-sandboxes-on-your-own-cks-cluster).
Then set these variables in the launching shell or Omnigent server process:

```bash
export OMNIGENT_CWSANDBOX_AUTH_STRATEGY=coreweave_api_key
export CWSANDBOX_API_KEY=...
export OMNIGENT_CWSANDBOX_PLACEMENT_MODE=cks
export OMNIGENT_CWSANDBOX_RUNNER_IDS=YOUR_RUNNER_ID
```

The runner's policy must allow the host image, 2 CPUs, 4 GiB of memory, the
configured sandbox lifetime, and connections to your Omnigent server and model
endpoints. If the policy's lifetime cap is below the launcher's 24-hour default,
adjust `OMNIGENT_CWSANDBOX_MAX_LIFETIME_S`. Runner IDs are comma-separated and
valid only with CKS placement. No serverless spillover is requested.

For direct SDK commands such as the stop example, use
`AuthStrategy.COREWEAVE_API_KEY` for these CoreWeave credentials.

A CoreWeave sandbox runner schedules sandboxes on CKS. The Omnigent runner
executes the agent inside each sandbox. They are separate components.

### Network access

With `OMNIGENT_CWSANDBOX_EGRESS_HOSTS` unset, the sandbox inherits its placement's
outbound network defaults. Serverless allows public outbound traffic,
including HTTP. On CKS, the runner policy defines the defaults.

Setting a nonempty `OMNIGENT_CWSANDBOX_EGRESS_HOSTS` **replaces those defaults with
an allowlist**. Only the listed HTTPS destinations are allowed. It doesn't add
them to the existing access.

```bash
export OMNIGENT_CWSANDBOX_EGRESS_HOSTS=your-host.example.com,api.openai.com,github.com
```

Replace these names with the destinations your workload needs. Use hostnames,
not URLs. This allowlist covers HTTPS and secure WebSockets on TCP port `443`.
Unlisted destinations and plain HTTP are blocked. Include every required
package, asset, and redirect host: for example, installing packages may need
both `pypi.org` and `files.pythonhosted.org`. On CKS, the runner policy must permit
the requested destinations. Unset the variable to restore placement defaults.

## The host image

Sandboxes boot from `ghcr.io/omnigent-ai/omnigent-host:latest`, published by CI
from the `host` target of [`deploy/docker/Dockerfile`](../docker/Dockerfile)
with Omnigent and its dependencies preinstalled — including the coding-harness
CLIs (`claude`, `codex`, `pi`, and `kiro-cli`). Other harnesses may need additional tools.

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

For a server with accounts authentication, complete the [sandbox login
step](#connecting-to-an-authenticated-server) before connecting. A login on
your laptop alone doesn't authenticate the in-sandbox host.

Set the externally reachable server URL, then create the sandbox:

```bash
export OMNIGENT_SERVER_URL=https://your-host
omnigent sandbox create --provider cwsandbox --server "$OMNIGENT_SERVER_URL"
```

This pulls the host image, builds wheels from your local checkout, and overlays
them on top — so the sandbox runs *your* code, not whatever the image was built
from. Copy the printed ID into `SANDBOX_ID`, then register it as a host:

```bash
export SANDBOX_ID=REPLACE_WITH_CREATED_SANDBOX_ID
omnigent sandbox connect --provider cwsandbox \
  --sandbox-id "$SANDBOX_ID" \
  --server "$OMNIGENT_SERVER_URL"
```

`connect` runs `omnigent host` inside the sandbox and holds the connection open
in your terminal. New sessions targeting that host run in the sandbox.
Ctrl-C stops the host process and disconnects it. The sandbox keeps running.
Stop it explicitly when you finish:

```bash
export SANDBOX_ID=REPLACE_WITH_CREATED_SANDBOX_ID
python -c 'import os; from cwsandbox import AuthStrategy, Sandbox; Sandbox.from_id(os.environ["SANDBOX_ID"], auth=AuthStrategy.WANDB).result().stop().result()'
```

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

For built-in accounts, authenticate inside the created sandbox before `connect`:

```bash
export SANDBOX_ID=REPLACE_WITH_CREATED_SANDBOX_ID
python deploy/cwsandbox/login.py \
  --sandbox-id "$SANDBOX_ID" --server "$OMNIGENT_SERVER_URL"
```

Enter your existing accounts username and password. The first admin username
normally comes from the OS user running the server, unless
`OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME` overrides it. Don't assume `admin`.

The helper sends credentials through SDK stdin to `omnigent login` inside the
sandbox. Accounts login needs no browser or callback port. It saves the normal
Omnigent login state, including refresh credentials when the server provides
them, so the host can authenticate on connect and reconnect. Then run the
`connect` command in [CLI-launched sandboxes](#cli-launched-sandboxes). Stopping the sandbox removes its login state.

For Databricks-fronted servers, inject ambient credentials before `create`:

```bash
export OMNIGENT_CWSANDBOX_SANDBOX_ENV=DATABRICKS_HOST,DATABRICKS_TOKEN
omnigent sandbox create --provider cwsandbox --server "$OMNIGENT_SERVER_URL"
```

Use either `DATABRICKS_TOKEN` or the `DATABRICKS_CLIENT_ID` and
`DATABRICKS_CLIENT_SECRET` pair with `DATABRICKS_HOST`. Browser-based OIDC login
still requires a supported callback path. This accounts helper doesn't provide
one. [Server-managed sandboxes](#server-managed-sandboxes) receive per-launch
credentials automatically and don't need this manual sandbox login.

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c config.yaml --no-open`,
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
itself needs `WANDB_API_KEY` (and optional `CWSANDBOX_BASE_URL`) in its
environment for the default W&B authentication path. For CKS, use the
[CoreWeave credential settings](#cks-sandboxes).

Sessions created with `host_type: "managed"` (the API call or the Web UI's New
Sandbox option) then run on a fresh CW sandbox; the create returns immediately
and provisioning happens in the background, exactly like the [Modal managed
flow](../modal/README.md#server-managed-sandboxes) — including repository
workspaces, the first-message rendezvous, and dead-sandbox relaunch.

For accounts authentication, first log in locally with your actual accounts username.
Capture the saved bearer into an environment variable for the API request:

```bash
export OMNIGENT_SERVER_URL=https://your-host
omnigent login "$OMNIGENT_SERVER_URL"
export OMNIGENT_API_TOKEN="$(python -c 'import os; from omnigent.cli_auth import load_token, refresh_stored_token; url = os.environ["OMNIGENT_SERVER_URL"]; token = refresh_stored_token(url) or load_token(url); assert token, "Run omnigent login first"; print(token)')"
curl --fail-with-body -X POST "$OMNIGENT_SERVER_URL/v1/sessions" \
  -H "Authorization: Bearer $OMNIGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "REPLACE_WITH_AGENT_ID", "host_type": "managed"}'
```

Choose an agent whose harness and model credentials are configured in the
sandbox. For a reproducible API-driven check, use the
[OpenAI probe](#managed-session-test) instead of a built-in native UI agent.

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

Managed hosts authenticate with a server-minted launch token. Their runners
use a separate binding token that the server maps to the session owner.
The built-in `accounts` provider supports this flow and can mint refreshable
owner bearer tokens for runner requests. You don't need to copy a user's
login token into a managed sandbox.

With header or OIDC-proxy authentication, configure the proxy to allow the
host and runner connections through to Omnigent. A proxy that requires its
own credentials can reject a connection before Omnigent validates its token.
See [server authentication](../README.md#auth).

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
- **Managed sandboxes share the server's sandbox account credentials.**
  The default W&B path uses `WANDB_API_KEY`. The CoreWeave path uses
  `CWSANDBOX_API_KEY`. Keep this account scoped to the workload. Users of the
  Omnigent server share its sandbox permissions and billing account.
- **The launch token's lifetime tracks the sandbox lifetime.** CW Sandbox's
  lifetime is operator-overridable (`OMNIGENT_CWSANDBOX_MAX_LIFETIME_S`, 24h
  default), so the per-launch host token TTL is derived from it — always above the
  cap by one hour, so a live sandbox can re-authenticate across reconnects.
  A relaunch mints a fresh one.

## Notes / limits

- Sandboxes are reaped at `max_lifetime_seconds` (24h default; override with
  `OMNIGENT_CWSANDBOX_MAX_LIFETIME_S`). The managed launch-token TTL is set above
  that so reconnects keep working.
- Serverless placement needs no cluster setup. CKS placement requires a ready
  runner and a policy permitting this workload. See [CKS sandboxes](#cks-sandboxes).
- To request specific HTTPS destinations, see [network access](#network-access).

## Troubleshooting

- **"managed host did not come online within 120s"** — the server waits up to two
  minutes for the in-sandbox host to register. If it times out, check that
  `server_url` is publicly reachable from CoreWeave, then inspect the in-sandbox
  host log: `/tmp/omnigent-host.log`.
- **CLI host exits with HTTP `403` / code `78` on an accounts server**: run the
  [sandbox login helper](#connecting-to-an-authenticated-server). A local login
  or `OMNIGENT_API_TOKEN` in your shell doesn't authenticate the remote host.
- **`create` fails while building wheels**: check `uv --version` and `PATH`.
  The shared bootstrap leaves an allocated sandbox running after a
  build failure. Stop the printed sandbox ID using the command in
  [CLI-launched sandboxes](#cli-launched-sandboxes) before retrying.
- **Slow first launch** — the first launch from a given image waits on a cold
  registry pull before the sandbox is ready; subsequent launches reuse the cached
  image and can start faster.
- **Agent has no credentials** — verify the injected var names match the
  forwarded set (or are named in `OMNIGENT_RUNNER_ENV_PASSTHROUGH`), and that each
  name was actually set in the launching environment.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `WANDB_API_KEY` | CLI machine / server | W&B API key for the default authentication path |
| `OMNIGENT_CWSANDBOX_AUTH_STRATEGY` | CLI machine / server | `wandb` (default) or `coreweave_api_key`. Selected explicitly for create and attach |
| `CWSANDBOX_API_KEY` | CLI machine / server | CoreWeave API access token. Required only with `coreweave_api_key` |
| `CWSANDBOX_BASE_URL` | CLI machine / server | Non-default CW Sandbox API endpoint (default `https://api.cwsandbox.com`) |
| `OMNIGENT_CWSANDBOX_HOST_IMAGE` | CLI machine / server | Override the host image ref (`sandbox.cwsandbox.image` takes precedence for managed) |
| `OMNIGENT_CWSANDBOX_SANDBOX_ENV` | CLI machine / server | Comma-separated launcher-side env var names to inject (`sandbox.cwsandbox.env` takes precedence for managed) |
| `OMNIGENT_CWSANDBOX_PLACEMENT_MODE` | CLI machine / server | `serverless` (default) or `cks` |
| `OMNIGENT_CWSANDBOX_RUNNER_IDS` | CLI machine / server | Comma-separated CKS runner IDs; requires `cks` placement |
| `OMNIGENT_CWSANDBOX_EGRESS_HOSTS` | CLI machine / server | HTTPS hostname allowlist that replaces default egress; omitted means placement defaults |
| `OMNIGENT_CWSANDBOX_MAX_LIFETIME_S` | CLI machine / server | Sandbox lifetime cap in seconds (default 24h); also derives the managed launch-token TTL |
| `OMNIGENT_RUNNER_ENV_PASSTHROUGH` | inside the sandbox (injected) | Extra env var names the host forwards to runners |
| `GIT_TOKEN` / `GIT_USERNAME` | inside the sandbox (injected) | HTTPS credentials for private repository clone / fetch / push |

## Smoke test

From the checkout root, validate SDK primitives with `AuthStrategy.WANDB`.
The test requests HTTPS access to `api.github.com`, creates a serverless sandbox,
and stops it on completion:

```bash
export WANDB_API_KEY=...
python tests/e2e/integrations/deploy/cwsandbox/smoke_test.py
```

This checks provision, exec, file upload, HTTPS egress, detached processes,
and stop. It doesn't exercise the Omnigent launcher or model credentials.
Also run the [CLI create/connect flow](#cli-launched-sandboxes) to verify the integration.

### Managed-session test

The included [probe agent](probe/agent.yaml) uses the `openai-agents` harness.
Set `OPENAI_API_KEY` on the server and replace `REPLACE_WITH_OPENAI_MODEL` in
the probe's `executor.model` with an OpenAI model available to your account.
Include the key in the sandbox environment list:

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://your-host
  cwsandbox:
    env: [OPENAI_API_KEY]
```

With that configuration saved as `config.yaml`, start the server from the checkout:

```bash
# Set OPENAI_API_KEY in this server's environment.
omnigent server -c config.yaml --no-open --agent deploy/cwsandbox/probe
```

Use your normal server bind/tunnel settings so `server_url` is reachable from
CoreWeave. The probe returns an assistant message through the API. Native
agents need their own CLI credentials and setup. The test doesn't choose one
arbitrarily.

From the test client, authenticate and run the probe:

```bash
omnigent login "$OMNIGENT_SERVER_URL"
python tests/e2e/integrations/deploy/cwsandbox/e2e_managed.py \
  --server "$OMNIGENT_SERVER_URL"
```

The script uses `OMNIGENT_API_TOKEN` if set, otherwise the credentials saved by
`omnigent login`. Unset an expired `OMNIGENT_API_TOKEN` to use the saved login.
It selects `e2e-probe` with the `openai-agents` harness, or the only agent with
that harness. With multiple candidates, pass `--agent-id` explicitly.

The test creates a managed session, waits for its host and completed assistant
reply, then deletes that session to stop its sandbox. Use `--keep` only to retain
test resources deliberately.

### Optional server hosted in a sandbox

The script's `--image` mode provisions a separate server sandbox and seeds a
W&B inference agent. Build an image containing this checkout and the SDK:

```bash
docker build -f deploy/docker/Dockerfile --target host --platform linux/amd64 \
  --build-arg OMNIGENT_EXTRAS=cwsandbox \
  -t docker.io/YOUR_NAMESPACE/omnigent-cwsandbox:test .
docker push docker.io/YOUR_NAMESPACE/omnigent-cwsandbox:test
# Set WANDB_API_KEY and WANDB_INFERENCE_KEY in your environment.
python tests/e2e/integrations/deploy/cwsandbox/e2e_managed.py \
  --image docker.io/YOUR_NAMESPACE/omnigent-cwsandbox:test
```

This mode creates a disposable accounts server with an explicit `admin` username
and a generated password. Its model credential (`WANDB_INFERENCE_KEY`) is
configured separately from the sandbox credential (`WANDB_API_KEY`). The server
and test session are removed when the test ends.
