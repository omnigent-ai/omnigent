# Omnigent on Islo

[Islo](https://islo.dev) sandboxes give you disposable cloud machines for
running Omnigent hosts, two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a
  sandbox from your terminal, ships your local checkout into it, and
  registers it as a host with your server.
- **Server-managed**: the server provisions a sandbox automatically when
  a session is created with `"host_type": "managed"` and terminates it
  when the session is deleted.

Sandboxes boot from the official prebaked host image, so startup is
seconds. Unlike Modal and Daytona, the Islo launcher talks to the Islo
HTTP API directly through `httpx` (already an Omnigent dependency), so
there is **no provider SDK extra to install** — just an API key.

What makes Islo different from the other providers, and shapes the rest
of this guide:

- **A credential gateway.** Islo can inject LLM/API keys into a sandbox's
  outbound traffic at the network layer, so the raw key never reaches the
  sandbox process. This is a first-class, recommended path for model
  credentials (see [Model credentials](#model-credentials-llm-keys)) and
  has no equivalent on Modal or Daytona.
- **No local port forward.** Islo can't forward a sandbox→laptop callback
  port, so the interactive in-sandbox `omnigent login` / App OAuth step is
  skipped automatically (as on Modal and Daytona).
- **No lifetime cap.** Islo sandboxes run until deleted (like Daytona,
  unlike Modal's 24 h).

## Prerequisites

Install the [Islo CLI](https://docs.islo.dev) and create an API key, then
make it available where the launcher runs — your shell for the CLI flow,
the **server** process for managed sandboxes:

```bash
curl -fsSL https://islo.dev/install.sh | sh   # install the islo CLI
islo login                                     # browser OAuth (one-time)
islo api-key create omnigent --show            # prints an islo_key_… value
export ISLO_API_KEY=islo_key_…
# Optional: a non-default API endpoint
# export ISLO_BASE_URL=https://api.islo.dev
```

`ISLO_API_KEY` is exchanged for a short-lived session token at
`POST /auth/token`; the token is cached until shortly before expiry. The
key is the only required credential — no SDK, no `~/.config` file.

> [!NOTE]
> **Islo cannot forward a local callback port into the sandbox.** The
> interactive `omnigent login` browser flow (and the in-sandbox App OAuth
> callback) needs a sandbox→laptop port forward, which Islo doesn't
> provide — so the CLI skips that step automatically, exactly as it does
> for Modal and Daytona. For a server that requires authentication, inject
> the credentials instead (see
> [Connecting to an authenticated server](#connecting-to-an-authenticated-server)).

## The host image

Sandboxes boot from `ghcr.io/omnigent-ai/omnigent-host:latest`, published
by CI from the `host` target of
[`deploy/docker/Dockerfile`](../docker/Dockerfile) with Omnigent and its
dependencies preinstalled — including the coding-harness CLIs (`claude`,
`codex`, `pi`, `kiro-cli`), so agents on any harness run without an in-sandbox
install.

To use a different image (a fork, or extra tooling baked in), build the
same target and push it anywhere Islo can pull from:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  --platform linux/amd64 \
  -t docker.io/<you>/omnigent-host:latest .
docker push docker.io/<you>/omnigent-host:latest
```

Then point Omnigent at it — `OMNIGENT_ISLO_HOST_IMAGE` for the CLI flow,
or `sandbox.islo.image` in the server config for the managed flow. For a
private registry, configure the pull credentials on the Islo side (Islo
pulls the image, not Omnigent).

> [!IMPORTANT]
> **Native terminals need `bubblewrap`.** The `claude-native` /
> `codex-native` / `kiro-native` / `pi` harnesses wrap each agent terminal in a bubblewrap
> (`bwrap`) OS-sandbox, and on Linux that isolation is mandatory and
> fail-loud — a host image without the `bwrap` binary makes those terminals
> fail to start (`linux_bwrap sandbox requires the 'bwrap' binary on PATH`).
> The `host` Dockerfile target installs `bubblewrap`; if you bring your own
> image, install it there too. See [Troubleshooting](#troubleshooting).

## CLI-launched sandboxes

Provision a sandbox and ship your local checkout into it:

```bash
omnigent sandbox create --provider islo
```

This pulls the host image, builds wheels from your local checkout, and
overlays them on top — so the sandbox runs *your* code, not whatever the
image was built from. Then register it as a host with your server:

```bash
omnigent sandbox connect --provider islo \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the
connection open in your terminal — Ctrl-C tears it down. New sessions
targeting that host now run in the sandbox.

Running multiple sandboxes against one server? Pass a unique
`--host-name <label>` to each `connect` — the server keys hosts on
(owner, name), and sandboxes that share a hostname collide.

Sandboxes are disposable. When your code changes, create a new one — and
delete the old one (Islo sandboxes have no lifetime cap, so an abandoned
sandbox keeps billing until removed via `islo rm <id>` or the
[dashboard](https://app.islo.dev)).

To inject LLM/git credentials into a CLI-launched sandbox, set
`OMNIGENT_ISLO_SANDBOX_ENV` in your shell to a comma-separated list of
variable names (e.g. `ANTHROPIC_API_KEY,GIT_TOKEN`) before running
`create` — the named variables are copied from your environment into the
sandbox at provision time. A listed name that is **not** set fails the
launch loudly (it would otherwise surface much later as an opaque harness
auth failure inside the sandbox).

### Connecting to an authenticated server

`connect` runs `omnigent host` inside the sandbox, and that host must
present credentials when it dials back to a server that requires
authentication. The interactive `omnigent login` browser flow can't run
inside an Islo sandbox (no callback port forward), so inject the keys for
the relevant server instead — name them in `OMNIGENT_ISLO_SANDBOX_ENV`
before `create`:

```bash
export OMNIGENT_ISLO_SANDBOX_ENV=DATABRICKS_HOST,DATABRICKS_TOKEN
omnigent sandbox create --provider islo
```

The in-sandbox host mints a fresh bearer token from those credentials on
every connect and reconnect. For a Databricks-fronted server, inject
`DATABRICKS_HOST` plus either `DATABRICKS_TOKEN` (a PAT) or
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` (an OAuth service
principal — re-minting keeps a long-lived sandbox connected past any
single token's expiry).

A server with no authentication on the host tunnel needs none of this,
and neither do [server-managed sandboxes](#server-managed-sandboxes) —
those authenticate with a server-minted per-launch token automatically.

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c
config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: islo
  server_url: https://your-host    # public URL sandboxes dial back to
```

`server_url` must be reachable *from Islo's cloud* — a public HTTPS URL,
not `localhost`. The server itself needs `ISLO_API_KEY` (and optional
`ISLO_BASE_URL`) in its environment. Sessions created with
`host_type: "managed"` (the API call or the Web UI's New Sandbox option)
then run on a fresh Islo sandbox; the create returns immediately and
provisioning happens in the background, exactly like the [Modal managed
flow](../modal/README.md#server-managed-sandboxes) — including repository
workspaces, the first-message rendezvous, and dead-sandbox relaunch.

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

Each managed sandbox authenticates back with a server-minted, per-launch
token (7-day TTL — see [Lifecycle](#lifecycle-notes)); no user
credentials enter the sandbox for the server connection.

### Managed hosts and server auth

How the dial-back authenticates depends on how the **server** does auth,
and there is one interaction worth knowing before you deploy. A managed
sandbox opens two kinds of connections back to the server:

- the **host tunnel** (`/v1/hosts/<id>/tunnel`), which the per-launch
  token authenticates directly — the server mints it, scopes it to one
  host, and resolves the owner from it. This always works.
- one **runner tunnel** per session (`/v1/runners/<token>/tunnel`), opened
  by the runner subprocess the host spawns. The runner authenticates with
  *whatever server credential it can resolve* — a proxy-injected identity
  (header / OIDC), or a stored `omnigent login` token (local hosts only; a
  fresh managed sandbox has none) — **not** the per-launch host token.

The consequence:

- **Header / OIDC-proxy auth, or single-user (no-auth) servers** — the
  runner tunnel needs no extra identity, so managed hosts work out of the
  box. **Verified end-to-end on a single-user server**: a session created
  with `host_type: "managed"` provisioned an Islo sandbox from the bwrap
  image, the launcher cleared the seeded `apiKeyHelper`, the host *and*
  runner tunnels connected, and a native Claude terminal ran on the
  injected `CLAUDE_CODE_OAUTH_TOKEN` subscription.
- **The built-in `accounts` provider (`OMNIGENT_AUTH_ENABLED=1`)** — the
  runner tunnel additionally requires a *user* identity, which the
  per-launch host token does not carry, so the runner dial-back is refused
  (`403`) even though the host tunnel connects. This is a framework-level
  managed-host interaction shared by **all** sandbox providers (Modal /
  Daytona / Islo), not specific to Islo.

So for a managed Islo deployment, front the server with **header or OIDC
auth** (a reverse proxy / IdP injects the user identity on every request,
including the runner WebSocket — see
[`deploy/README.md#auth`](../README.md#auth)), or run it single-user. The
`accounts` provider is fine for CLI-launched hosts (you `omnigent login`,
and that token is what the in-sandbox host forwards), but not yet for the
managed runner dial-back.

Optional `islo:` settings:

```yaml
sandbox:
  provider: islo
  server_url: https://your-host
  islo:
    image: docker.io/<you>/omnigent-host:latest   # default: official image
    env: [OPENAI_API_KEY, GIT_TOKEN]               # copy from server env
    base_url: https://api.islo.dev                 # non-default API endpoint
    gateway_profile: default                       # Islo gateway for egress + credential injection
    snapshot_name: warm-host                       # boot from a prebaked snapshot
    workdir: /root/workspace                       # sandbox working directory
    vcpus: 2
    memory_mb: 4096
    disk_gb: 20
```

## Model credentials (LLM keys)

A fresh sandbox has no model credentials of your own. Islo offers **two
distinct ways** to give the agent a model — and they interact, so pick one
deliberately per harness.

### Option A — Islo gateway integration (recommended)

This is the Islo-native path with no Modal/Daytona equivalent. Islo
[gateways](https://docs.islo.dev/cli/gateways) "automatically attach API
keys, tokens, and secrets to outbound requests" at the **network layer** —
*"credentials never reach the sandbox process."* You connect a provider
once, server-side, and every sandbox picks it up:

```bash
islo login --tool claude     # OAuth-connect Anthropic (alias: --tool anthropic)
islo login --tool openai     # …and/or OpenAI
islo status                  # shows connected integrations
```

This is **not Claude-specific** — it's how Islo supplies model
credentials to *every* harness. Islo pre-seeds each sandbox with a
**phantom** placeholder key (`islo_phantom_…`) in whatever location the
harness reads, per provider:

| Harness | Phantom key location | Provider endpoint |
|---|---|---|
| Claude Code (`claude-native`, `claude-sdk`, `pi`) | `apiKeyHelper` in `~/.claude/settings.json` | `api.anthropic.com` |
| Codex / OpenAI agents | `OPENAI_API_KEY` env var | `api.openai.com` |

The harness sends that placeholder to its provider endpoint; the gateway
intercepts the request and swaps it for your connected credential before
forwarding. The raw key never lands in the sandbox, and the connection is
team-wide — other members don't need their own. (We observed both phantom
keys pre-seeded in a single sandbox.)

These integrations connect **provider API keys** (per-token billing), not
plan/subscription auth — `--tool claude` gives an Anthropic API key, not a
Claude Pro/Max subscription; `--tool openai` gives an OpenAI API key, not
a ChatGPT plan. To use a subscription or plan token on any harness (a
Claude Pro/Max token, a Codex access token), use
[Option B](#option-b--omnigent-env-injection-your-own-key-or-a-subscription).

> [!IMPORTANT]
> If `islo status` shows **"No integrations connected"** for a provider,
> its phantom key resolves to nothing — the harness falls back to a failing
> request (Claude reports "API Usage Billing" and retries). Connect the
> integration for each provider whose harness you use.

#### Path A under managed hosts

This is where the gateway shines: when the **server** launches sandboxes,
you configure **no model credential on the Omnigent side at all**. The
flow:

```
admin (once):  islo login --tool claude   → connects Anthropic to the Islo ACCOUNT
                                                              │
server ──ISLO_API_KEY──▶ Islo API "create sandbox" ──▶ sandbox under that account
                                                              │ Islo pre-seeds the
                                                              │ phantom apiKeyHelper
                                                              ▼
   agent's claude → api.anthropic.com (phantom key) ──▶ Islo gateway swaps in the real key
```

The Omnigent server only ever holds `ISLO_API_KEY` — the credential it
uses to *create* sandboxes. Because every managed sandbox is created under
that Islo account, and integrations are connected at the **account/team**
level, each one inherits the connected Claude credential through the
gateway automatically. The only Omnigent-side knob is which gateway a
managed sandbox uses:

```yaml
sandbox:
  provider: islo
  server_url: https://your-host
  islo:
    gateway_profile: default     # the Islo gateway carrying the connected integration
```

Two consequences worth internalizing:

- **No model secret lives in the Omnigent server's config or
  environment** — nothing to leak there. Contrast [Option B under managed
  hosts](#option-b--omnigent-env-injection-your-own-key-or-a-subscription),
  where the key sits in `sandbox.islo.env` (copied from the server's env
  into each sandbox).
- **The integration must be connected on the same Islo account the
  server's `ISLO_API_KEY` belongs to.** If your server runs under a
  dedicated service/CI Islo account, run `islo login --tool claude` while
  authenticated as *that* account — not a personal laptop login.

### Option B — Omnigent env injection (your own key or a subscription)

Bring your own credential by naming it in `OMNIGENT_ISLO_SANDBOX_ENV`
(CLI) or `sandbox.islo.env` (managed); the launcher copies the value from
the launching environment into the sandbox, and the in-sandbox host
forwards the standard harness credential vars to its runners:

| Variable | Enables |
|---|---|
| `ANTHROPIC_API_KEY` | Claude models on the Anthropic API |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | Anthropic-compatible gateways (LiteLLM, Bedrock/Vertex bridges, corporate proxies) |
| `CLAUDE_CODE_OAUTH_TOKEN` | claude-code with a Claude **subscription** (no API key) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI or any OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, …) |
| `CODEX_ACCESS_TOKEN` | codex with a ChatGPT Business/Enterprise workspace |
| `GEMINI_API_KEY` | Gemini on the Google AI API |

The full per-plan recipes (subscriptions, gateways, open-source models)
are identical to Modal — see the [variable table and
recipes](../modal/README.md#llm-credentials-for-managed-sandboxes). For a
Claude **subscription** specifically, run `claude setup-token` on your own
machine (one-time browser auth) and inject the resulting long-lived token
as `CLAUDE_CODE_OAUTH_TOKEN`. For env vars beyond the standard set, inject
`OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

> [!NOTE]
> **Your injected Claude credential automatically wins over Islo's phantom
> helper.** Islo seeds an `apiKeyHelper` into every sandbox, and Claude Code
> would normally prefer it over a `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`
> in the environment. So when you inject one of those, the Islo launcher
> strips the seeded `apiKeyHelper` at provision time — for **both**
> CLI-launched and server-managed sandboxes — leaving your credential as the
> sole auth path. No manual step, nothing to run inside the sandbox.

Codex/OpenAI needs nothing special either — its phantom is the
`OPENAI_API_KEY` env var, which your injected `OPENAI_API_KEY` simply
overrides.

### Choosing between A and B

| | Option A — gateway | Option B — env injection |
|---|---|---|
| Credential | provider **API key** (Anthropic, OpenAI, …) | your API key **or** subscription/plan token |
| Billing | per-token API | API key, or your subscription/plan |
| Key in sandbox? | **No** (gateway injects out-of-band) | Yes (in the sandbox env) |
| Scope | account/team-wide, all sandboxes | per-sandbox |
| Managed-host config | just `gateway_profile`; no secret on server | key in `sandbox.islo.env` |
| Best for | **managed / production**, API-key billing | your own **subscription** or key; CLI or managed |
| Catch | needs a connected integration | the injected var must be set where the launcher runs |

Pick **one per harness** — connect Islo's integration *or* inject your own
credential, never both. Both work in either launch flow: the launcher
strips Islo's seeded `apiKeyHelper` automatically when you inject a Claude
credential. The same two patterns apply to Codex/OpenAI (`islo login --tool
openai`, or inject `OPENAI_API_KEY` / `CODEX_ACCESS_TOKEN`).

### Git credentials (private repositories)

Inject an HTTPS token as `GIT_TOKEN` (GitLab: add `GIT_USERNAME=oauth2`)
via `OMNIGENT_ISLO_SANDBOX_ENV` / `sandbox.islo.env`. The host image's git
credential helper answers HTTPS auth from it for both the launch-time
clone and the agent's later `fetch` / `push`, writing nothing to disk. Use
HTTPS repository URLs. Details by provider match the [Modal git
guide](../modal/README.md#git-credentials-private-repositories).

## Security considerations

- **Path A keeps the model key out of the sandbox — a real advantage.**
  With the gateway, the agent process only ever sees the phantom
  placeholder; the real key is injected at Islo's network edge. A
  prompt-injected or compromised agent can't exfiltrate a key it never
  holds. This is the strongest credential posture of the three providers
  for model keys, and the reason to prefer Option A where API-key billing
  is acceptable.
- **The gateway terminates TLS to inject.** Credential injection means
  Islo's gateway sits in the path of the agent's outbound LLM traffic and
  re-originates it — so that traffic (prompts, completions, tool output
  sent to the model) is visible at Islo's edge. Acceptable for most teams,
  but weigh it for highly sensitive workloads, and scope the
  `gateway_profile` to exactly the egress you intend.
- **Option B puts the key in the sandbox.** A subscription token or API key
  named in `sandbox.islo.env` is copied into the sandbox environment at
  provision time and lives there for the sandbox's life. Prefer scoped,
  short-lived credentials, and rely on the per-terminal `bwrap` sandbox
  (below) to keep the agent away from it.
- **The agent terminal is sandboxed away from those secrets.** Native
  harness terminals run under a bubblewrap OS-sandbox that masks dotfiles
  (`~/.ssh`, `~/.aws`, the injected `~/.omnigent` server token) and pins
  the agent to its workspace — defense-in-depth *inside* the Islo sandbox,
  independent of Islo's own isolation. This is why the image must ship
  `bwrap` (see [the host image](#the-host-image)).
- **All managed sandboxes share one Islo org + `ISLO_API_KEY`.**
  Cross-user isolation rides on Islo's sandbox boundaries, and the shared
  org key can enumerate and delete any sandbox — the same single-tenant-org
  shape as the Modal and Daytona providers. Scope the org to this workload.
- **The launch token's lifetime is 7 days.** Islo sandboxes have no
  platform lifetime cap, so the per-launch host token must outlive a
  long-running sandbox across reconnects (a longer replay window than
  Modal's ~24 h; same as Daytona). A relaunch mints a fresh one.

## Lifecycle notes

- **No platform lifetime cap.** Unlike Modal's 24-hour limit, Islo
  sandboxes run until deleted. The managed flow deletes a sandbox when its
  session is deleted, and the dead-sandbox relaunch path replaces one that
  crashed or was removed out-of-band. CLI-launched sandboxes you delete
  yourself (`islo rm <id>`).
- **Resources.** Sandboxes default to 2 vCPUs and 4 GiB of memory;
  override per managed launch with `vcpus` / `memory_mb` / `disk_gb`.
- **Warm starts.** Set `sandbox.islo.snapshot_name` to boot from a
  prebaked Islo snapshot instead of a cold image pull.
- **Provider-side lifecycle** (list / status / delete / stop) — use the
  `islo` CLI (`islo ls`, `islo rm <id>`) or the
  [dashboard](https://app.islo.dev) directly.

## Cost

Islo bills usage with no seat licenses or idle fees: ~$0.07/CPU-hour,
~$0.04/GB-hour of memory, ~$0.0007/GB-hour of disk — about $0.25/hour for
the default 2 vCPU / 4 GiB sandbox while it runs. New accounts get $50 of
free credits. Rates: [islo.dev](https://islo.dev).

## Troubleshooting

- **Native Claude/Codex terminal fails with `linux_bwrap sandbox requires
  the 'bwrap' binary on PATH`.** The native harnesses wrap each agent
  terminal in a bubblewrap OS-sandbox; the host image must ship
  `bubblewrap`. The `host` Dockerfile target installs it — rebuild from a
  current image, or for a one-off on a CLI-launched sandbox run
  `apt-get install -y bubblewrap` inside it.
- **Claude shows "API Usage Billing" / "both `CLAUDE_CODE_OAUTH_TOKEN` and
  `apiKeyHelper` set."** You injected your own Claude credential (Option B)
  but Islo's phantom `apiKeyHelper` is still present — the launcher strips it
  automatically at provision, so this means the strip didn't run: confirm the
  credential is named in `OMNIGENT_ISLO_SANDBOX_ENV` / `sandbox.islo.env` (the
  signal the launcher keys on), and check the provision log for the
  "clearing Islo's seeded apiKeyHelper" line.
- **Requests retry then fail with no obvious error.** `islo status` shows
  no connected integration, so the phantom `apiKeyHelper` resolves to
  nothing. Connect one (Option A) or switch to Option B.
- **"managed host did not come online within 120s."** Check that
  `server_url` is publicly reachable from Islo's cloud, then inspect the
  in-sandbox host log: `~/.omnigent/logs/host-runner/*.log`.
- **Agent has no credentials.** Verify the injected var names match the
  forwarded set above (or are named in `OMNIGENT_RUNNER_ENV_PASSTHROUGH`),
  and that each name was actually set in the launching environment.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `ISLO_API_KEY` | CLI machine / server | Islo API credentials (required) |
| `ISLO_BASE_URL` | CLI machine / server | Non-default Islo API endpoint (default `https://api.islo.dev`) |
| `OMNIGENT_ISLO_HOST_IMAGE` | CLI machine / server | Override the host image ref (`sandbox.islo.image` takes precedence for managed) |
| `OMNIGENT_ISLO_SANDBOX_ENV` | CLI machine / server | Comma-separated launcher-side env var names to inject (`sandbox.islo.env` takes precedence for managed) |
| `OMNIGENT_RUNNER_ENV_PASSTHROUGH` | inside the sandbox (injected) | Extra env var names the host forwards to runners |
| `GIT_TOKEN` / `GIT_USERNAME` | inside the sandbox (injected) | HTTPS credentials for private repository clone / fetch / push |
