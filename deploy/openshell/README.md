# Omnigent on NVIDIA OpenShell

[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) is a self-hosted sandbox
provider. Omnigent connects to an OpenShell **gateway** with the official
[`openshell`](https://pypi.org/project/openshell/) Python SDK and asks that
gateway to create, execute in, and delete sandboxes on the gateway's configured
compute driver.

This guide covers the Omnigent-specific OpenShell setup:

- install the `openshell` extra;
- select a working OpenShell gateway;
- use an OpenShell-compatible Omnigent host image;
- configure CLI-launched or server-managed sandboxes.

```bash
pip install 'omnigent[openshell]'
```

Omnigent uses OpenShell two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a sandbox
  from your terminal, ships your local checkout into it, and registers it as a
  host with your server.
- **Server-managed**: the server provisions a sandbox automatically when a
  session is created with `"host_type": "managed"` and terminates it when the
  session is deleted.

This is a sandbox-provider guide, not a server deploy target.

Two traits shape the rest of this guide:

- **gRPC, and a gateway you select — not an API key.** Omnigent connects through
  the OpenShell gateway you've made active with `openshell gateway select`. The
  SDK's `from_active_cluster()` resolves that gateway's endpoint, TLS material,
  and OIDC token from `$OPENSHELL_GATEWAY` / `~/.config/openshell/active_gateway`.
  There is no base-URL or token knob in Omnigent — gateway setup and auth are an
  OpenShell concern.
- **No local port forward.** OpenShell has no sandbox→laptop callback path, so
  the interactive in-sandbox `omnigent login` / App OAuth step is skipped
  automatically (as on Modal, Daytona, and CoreWeave) — fine for token/OIDC-auth
  servers.

## Prerequisites

You need a **running OpenShell gateway** with a compute driver, made active on
the machine the launcher runs on. Installing and operating the gateway is an
OpenShell concern — follow the
[OpenShell docs](https://docs.nvidia.com/openshell). Install the runtime + CLI:

```bash
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
```

(Apple Silicon macOS installs the Homebrew formula; Linux installs the deb/rpm.)

> [!IMPORTANT]
> **The gateway host must be amd64 Linux.** OpenShell's supervisor
> (Landlock/seccomp/netns) does not run reliably under emulation — on an arm64
> host (e.g. Apple Silicon via colima) the sandbox never reaches READY. The
> official host image now publishes multi-arch (amd64 + arm64), but its arm64
> variant omits `cel-expr-python` (no linux-arm64 wheel — CEL policies degrade to
> unavailable there), so the amd64 variant is the one to run with OpenShell. On an
> Apple-Silicon laptop, point the gateway at a remote **amd64 Linux** box (and the
> server at that gateway) rather than the local Docker VM.

### Minimal local Docker gateway (for trying it out)

For a quick local test, run one OpenShell gateway backed by your local Docker
daemon. The gateway needs a signing key so sandbox containers can authenticate
back to it; the helper script creates that key, writes the gateway config, starts
the gateway, registers it with the OpenShell CLI, and waits for `openshell status`
to report `Connected`.

Make sure Docker is running first. If you use colima, set `DOCKER_HOST` before
running the script:

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
```

Then start and register the gateway:

```bash
deploy/openshell/start-local-docker-gateway.sh
```

The script writes local development state under `~/.openshell-local` and leaves
gateway logs at `~/.openshell-local/gateway.log`.

For a real deployment, run the gateway behind TLS with OIDC or mTLS (see the
OpenShell docs), then `openshell gateway add <https-url>` and `openshell gateway
login`; the SDK picks up the TLS/OIDC material from the gateway metadata
automatically — Omnigent needs no extra configuration.

> [!WARNING]
> `allow_unauthenticated_users = true` and `--disable-tls` are local-development
> conveniences. Don't expose such a gateway on a network.

## The host image

Sandboxes boot from `ghcr.io/omnigent-ai/omnigent-host:latest`, published by CI
from the `host` target of [`deploy/docker/Dockerfile`](../docker/Dockerfile) with
Omnigent and its dependencies preinstalled — including the coding-harness CLIs
(`claude`, `codex`, `pi`), so agents on any harness run without an in-sandbox
install. OpenShell injects its own supervisor as the container entrypoint.

The `host` target also carries the two things OpenShell's image contract requires
(and which are inert for the root-based providers): a non-root **`sandbox`
user/group** and **`iproute2`/`nftables`** for the per-sandbox network namespace.
A custom image used with OpenShell must include both, or the supervisor refuses to
start. (The launcher handles the remaining non-root detail — pinning each exec's
cwd and `$HOME` to `/home/sandbox` — so the image's `/root` default still works
for the other providers.)

Before using an image with OpenShell, smoke-test that contract from the same
Docker daemon the gateway uses:

```bash
docker run --rm --entrypoint sh ghcr.io/omnigent-ai/omnigent-host:latest \
  -lc 'id sandbox && command -v ip && command -v nft'
```

To use a different image (a fork, or extra tooling baked in), run the build from
an Omnigent repository checkout on an amd64 Docker-capable machine, then push it
where the gateway's driver can pull from:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  --platform linux/amd64 \
  -t docker.io/<you>/omnigent-host:latest .
docker push docker.io/<you>/omnigent-host:latest
```

Then point Omnigent at it with `OMNIGENT_OPENSHELL_HOST_IMAGE`.

> [!NOTE]
> **Air-gapped?** Pre-load the host image (and OpenShell's supervisor image) into
> the registry or host the gateway pulls from — the first launch from an uncached
> image otherwise waits on a registry pull.

## CLI-launched sandboxes

With a gateway selected, provision a sandbox and ship your local checkout into
it:

```bash
omnigent sandbox create --provider openshell --server https://your-host
```

This creates a sandbox from the host image, builds wheels from your local
checkout, and overlays them on top — so the sandbox runs *your* code, not
whatever the image was built from. Then register it as a host with your server:

```bash
omnigent sandbox connect --provider openshell \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the connection open
in your terminal — Ctrl-C tears it down (stopping the in-sandbox host). New
sessions targeting that host now run in the sandbox. Pass a unique `--host-name
<label>` per sandbox when connecting several to one server (the server keys hosts
on (owner, name)). Sandboxes are disposable; when your code changes, create a new
one.

To inject LLM/git credentials into the sandbox, set `OMNIGENT_OPENSHELL_SANDBOX_ENV`
in your shell to a comma-separated list of variable names before running
`create` — the named variables are copied from your environment into the sandbox
at provision time. A listed name that is **not** set fails the launch loudly (it
would otherwise surface much later as an opaque harness auth failure inside the
sandbox):

```bash
export OMNIGENT_OPENSHELL_SANDBOX_ENV=ANTHROPIC_API_KEY,GIT_TOKEN
omnigent sandbox create --provider openshell --server https://your-host
```

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c config.yaml`,
or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: openshell
  server_url: https://your-host    # public URL sandboxes dial back to
```

`provider` + `server_url` is a complete config. Sessions created with
`host_type: "managed"` (the API call or the Web UI's New Sandbox option) then run
on a fresh OpenShell sandbox; the create returns immediately and provisioning
happens in the background, exactly like the [Modal managed
flow](../modal/README.md#server-managed-sandboxes). Each managed sandbox
authenticates back with a server-minted, per-launch token — no user credentials
enter the sandbox for the server connection.

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

Unlike the cloud providers, OpenShell needs no API key in the server environment —
the **server process** must instead have OpenShell gateway access: it connects
with the same `from_active_cluster()` resolution as the CLI, so select a gateway
with `openshell gateway select` (or set `OPENSHELL_GATEWAY` /
`sandbox.openshell.cluster`) where the server runs. `server_url` must be reachable
**from the sandbox** — and because OpenShell is deny-by-default on egress, that
reachability is not automatic; see [Network egress policy](#network-egress-policy).

Optional `openshell:` settings:

```yaml
sandbox:
  provider: openshell
  server_url: https://your-host
  openshell:
    image: docker.io/<you>/omnigent-host:latest         # default: official image
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]  # server env var NAMES to inject
    cluster: my-gateway                                  # default: active gateway
```

How the managed dial-back interacts with the server's auth mode is a
framework-level behavior shared by all providers; see
[`deploy/cwsandbox/README.md`](../cwsandbox/README.md#managed-hosts-and-server-auth).

## Network egress policy

This is the part of an OpenShell deployment most likely to trip you up. OpenShell
is **deny-by-default**: every sandbox runs in its own network namespace with all
egress forced through a policy proxy, and anything not explicitly allowed is
blocked (the in-sandbox `https_proxy` returns `403`). The agent and host run with
*no* outbound access until the sandbox policy grants it. The policy is resolved
from `/etc/openshell/policy.yaml` baked into the image, or set per-sandbox; see
the [OpenShell policy schema](https://docs.nvidia.com/openshell). A managed host
needs egress to:

- **the server URL** (`server_url`) — the host and runner dial it back over a
  WebSocket tunnel; without it the host can connect but the runner never registers;
- **the LLM provider host** — the agent's model calls originate *inside* the
  sandbox (e.g. `*.googleapis.com` for Gemini, `api.anthropic.com` for Claude,
  `api.openai.com` for OpenAI);
- **tokenizer/asset hosts** some harnesses fetch on first use, e.g.
  `*.blob.core.windows.net` (the openai-agents harness downloads the `tiktoken`
  encoding).

A minimal `network_policies` block (in the image's `policy.yaml`) looks like:

```yaml
network_policies:
  server:
    endpoints: [{ host: "your-host.example.com", port: 443, tls: skip }]
    binaries:  [{ path: /** }]
  llm:
    endpoints: [{ host: "*.googleapis.com", port: 443, tls: skip }]
    binaries:  [{ path: /** }]
```

> [!IMPORTANT]
> **Forward the proxy vars to the runner.** The host inherits the sandbox's
> `https_proxy`/`http_proxy`, but the runner subprocess it spawns does **not** —
> so the runner fails with `Temporary failure in name resolution` even though the
> host connected. Inject `OMNIGENT_RUNNER_ENV_PASSTHROUGH` naming the proxy vars so
> the host forwards them:
> ```yaml
> sandbox:
>   openshell:
>     env: [OMNIGENT_RUNNER_ENV_PASSTHROUGH, …]   # value set in the server env:
> # OMNIGENT_RUNNER_ENV_PASSTHROUGH=https_proxy,http_proxy,HTTPS_PROXY,HTTP_PROXY,NO_PROXY,no_proxy
> ```

> [!TIP]
> For LLM traffic specifically, OpenShell recommends its **inference routing**
> over allow-listing the provider host directly, so a stolen key can't be used to
> reach the provider from inside the sandbox. The allow-list above is the simplest
> path to get a turn working; inference routing is the hardened one.

## Model credentials (LLM keys)

A fresh sandbox has no model credentials. Name the variables to inject in
`OMNIGENT_OPENSHELL_SANDBOX_ENV`; the launcher copies the value from your
environment into the sandbox, and the in-sandbox host forwards the standard
harness credential vars (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `GEMINI_API_KEY`, …) to its runners.

```bash
export ANTHROPIC_API_KEY=sk-ant-…
export OMNIGENT_OPENSHELL_SANDBOX_ENV=ANTHROPIC_API_KEY
```

Which variables to inject — providers, gateways, subscriptions — is identical to
the other providers; see the [Modal variable table and per-plan
recipes](../modal/README.md#llm-credentials-for-managed-sandboxes). For a Claude
**subscription**, run `claude setup-token` on your own machine (one-time browser
auth) and inject the resulting `CLAUDE_CODE_OAUTH_TOKEN`. For env vars beyond the
standard set, inject `OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

> [!TIP]
> OpenShell can also enforce credential and egress policy at the sandbox boundary
> via its declarative YAML policy (a gateway-side feature, independent of
> Omnigent). See the [OpenShell policy docs](https://docs.nvidia.com/openshell).

## Git credentials (private repositories)

Inject an HTTPS token as `GIT_TOKEN` (GitLab: add `GIT_USERNAME=oauth2`) via
`OMNIGENT_OPENSHELL_SANDBOX_ENV`. The host image's git credential helper answers
HTTPS auth from it for both the launch-time clone and the agent's later `fetch` /
`push`, writing nothing to disk. Use HTTPS repository URLs. Details by provider
match the [Modal git guide](../modal/README.md#git-credentials-private-repositories).

## How it works

- **Connection.** `OpenShellSandboxLauncher` builds a `SandboxClient` via
  `from_active_cluster()` and calls the gateway over gRPC: `CreateSandbox` +
  `wait_ready` to provision, `ExecSandbox` to run commands, `DeleteSandbox` to
  terminate.
- **File shipping.** OpenShell exposes command execution but no upload RPC, so
  `put` streams the file's bytes to `cat` over the exec channel's stdin (the same
  approach NVIDIA's own LangChain backend uses). Wheels are shipped this way, then
  installed with the shared host-image overlay command.
- **Sandbox identity.** OpenShell assigns each sandbox a petname (e.g.
  `touched-urial`); that name is the handle Omnigent prints and reuses. The
  requested `--name` is advisory.
- **Non-root execution.** OpenShell runs the agent as the `sandbox` user, so the
  launcher pins every exec's cwd and `$HOME` to `/home/sandbox` (the image keeps
  `/root` as its default for the root-based providers).
- **Long-lived host.** OpenShell terminates an exec's process tree the moment the
  exec returns, so the in-sandbox host can't be detached with the usual
  `setsid nohup … &` (it gets reaped instantly). The launcher instead runs it as a
  foreground exec held open on a daemon thread for the session's lifetime.

## Troubleshooting

- **`docker sandboxes require gateway JWT auth; configure [openshell.gateway.gateway_jwt]`**
  — the Docker driver needs a gateway-minted sandbox JWT. Generate the Ed25519
  key material and add the `[openshell.gateway.gateway_jwt]` block as shown in
  [Minimal local Docker gateway](#minimal-local-docker-gateway-for-trying-it-out),
  then restart the gateway.
- **`No OpenShell server configured` / `Could not connect to an OpenShell gateway`**
  — no gateway is active. Run `openshell gateway select <name>` (or set
  `OPENSHELL_GATEWAY`), and confirm with `openshell status`.
- **Sandbox stuck in `Provisioning`** — usually a slow first image pull. Confirm
  the gateway's Docker daemon can pull the host image (`docker pull <image>` from
  the same `DOCKER_HOST`); pre-pull it to cache. On colima, make sure the gateway
  was started with `DOCKER_HOST` pointed at colima's socket — `/var/run/docker.sock`
  may point at a different (stopped) Docker.
- **Agent has no credentials** — verify the injected var names match the forwarded
  set (or are named in `OMNIGENT_RUNNER_ENV_PASSTHROUGH`), and that each name was
  actually set in the launching environment.
- **Host registers but the runner never comes online / runner log shows
  `Temporary failure in name resolution`** — the runner subprocess isn't getting
  the sandbox's proxy vars. Forward them with `OMNIGENT_RUNNER_ENV_PASSTHROUGH`
  (see [Network egress policy](#network-egress-policy)).
- **Turn fails reaching the model, or proxy returns `403`** — the destination
  isn't in the sandbox's egress allow-list. Add the LLM host (and any
  tokenizer/asset host) to `network_policies` (see
  [Network egress policy](#network-egress-policy)).
- **Sandbox container restarts / `sandbox user 'sandbox' not found` or
  `trusted ip helper not found`** — the image isn't OpenShell-compatible. Use the
  official host image (or include the `sandbox` user + `iproute2` in your custom
  one); see [The host image](#the-host-image).

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `OPENSHELL_GATEWAY` | CLI machine / server | Gateway name to use; overrides `~/.config/openshell/active_gateway` (read by the SDK). `sandbox.openshell.cluster` takes precedence for managed. |
| `OMNIGENT_OPENSHELL_HOST_IMAGE` | CLI machine | Override the host image ref (default `ghcr.io/omnigent-ai/omnigent-host:latest`); `sandbox.openshell.image` is the managed equivalent |
| `OMNIGENT_OPENSHELL_SANDBOX_ENV` | CLI machine | Comma-separated launcher-side env var names to inject into the sandbox; `sandbox.openshell.env` is the managed equivalent |
| `OMNIGENT_RUNNER_ENV_PASSTHROUGH` | inside the sandbox (injected) | Extra env var names the host forwards to runners |
| `GIT_TOKEN` / `GIT_USERNAME` | inside the sandbox (injected) | HTTPS credentials for private repository clone / fetch / push |

## Validation

Exercised end-to-end against a live OpenShell gateway on an **amd64 Linux** host
(Docker driver, the official host image):

- **Launcher primitives** — provision → run (`echo` / `uname`) → put (file upload
  over exec stdin) → verify → terminate, plus `exec_foreground` (the `connect`
  primitive) streaming output and propagating exit codes; the gateway logs the
  matching `CreateSandbox` / `ExecSandbox` / `DeleteSandbox` RPCs.
- **Full server-managed session** — a `host_type:"managed"` session drove the
  server to provision a sandbox on the gateway, start `omnigent host` in it (held
  foreground exec), dial back over the tunnel, register, spawn the runner, and
  complete a real agent turn (a Gemini model via the openai-agents harness) — the
  agent's reply came back from inside the sandbox.

Unit tests (a faked SDK / launcher, no gateway needed) cover provision, run, file
upload, foreground streaming, attach, terminate, env passthrough, error handling,
and the managed-config parsing:

```bash
pip install -e '.[openshell,dev]'
pytest tests/onboarding/sandboxes/test_openshell.py tests/server/test_managed_hosts.py
```
