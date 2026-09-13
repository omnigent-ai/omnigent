# Omnigent on E2B

[E2B](https://e2b.dev) sandboxes give you disposable cloud machines for
running Omnigent hosts, two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a
  sandbox from your terminal, ships your local checkout into it, and
  registers it as a host with your server.
- **Server-managed**: the server provisions a sandbox automatically when
  a session is created with `"host_type": "managed"` and terminates it
  when the session is deleted.

> [!IMPORTANT]
> **E2B boots from a pre-built *template*, not a registry image.** Unlike
> the Modal / Daytona / CoreWeave launchers — which pull
> `ghcr.io/omnigent-ai/omnigent-host` directly — E2B cannot start an
> arbitrary registry image at create time. You must first build the
> Omnigent host image into an E2B template (a one-time step, below); the
> launcher's `template` field then names *that template*, not a
> `ghcr.io/...` reference. This is the one real difference from the other
> sandbox providers. This directory is **not** a server deploy target.

## Prerequisites

```bash
pip install 'omnigent[e2b]'   # installs the e2b SDK extra
npm i -g @e2b/cli             # the E2B CLI, for building the template
```

Create an API key in the [E2B dashboard](https://e2b.dev/dashboard) and
make it available where the launcher runs — your shell for the CLI flow,
the **server** process for managed sandboxes:

```bash
export E2B_API_KEY=e2b_…
e2b auth login                # one-time, authenticates the E2B CLI too
```

> [!NOTE]
> **Lifetime is capped and cannot be disabled.** An E2B sandbox carries a
> single timeout (default 5 minutes; account maximum **24 h on Pro, 1 h on
> Hobby**) with no "never expire" option. Omnigent requests the 24 h
> maximum at creation, but E2B **rejects** (does not clamp) a request above
> the account cap, so `provision` automatically **retries clamped to the
> account's maximum** (e.g. 1 h on Hobby) — verified live. Set
> `OMNIGENT_E2B_MAX_LIFETIME_S` to request a specific lifetime and skip the
> retry. A managed session outliving the cap relies on the dead-sandbox
> relaunch path (same posture as Modal's 24 h limit), so a **Pro account**
> is recommended for anything beyond short demos.

## Build the host template (one time)

E2B builds a template from a Dockerfile whose base image must be
**Debian-based** and **single-stage**. The Omnigent host image
(`python:slim`, Debian) satisfies both — so the template Dockerfile is a
one-liner that layers nothing on top of the published image:

```bash
mkdir -p omnigent-e2b && cd omnigent-e2b
cat > e2b.Dockerfile <<'EOF'
# Single-stage, Debian-based — both E2B requirements. The host image
# already bakes the full omnigent install plus git / tmux / curl, so
# nothing else is needed here.
FROM ghcr.io/omnigent-ai/omnigent-host:latest
EOF

e2b template build --name omnigent-host --dockerfile e2b.Dockerfile
```

`omnigent-host` is the default template name the launcher looks for
([`DEFAULT_E2B_TEMPLATE`](../../omnigent/onboarding/sandboxes/e2b.py)), so
a deployment that uses that name needs no further config. Use a different
name (or pin a `:sha-<short>` host image) and point the launcher at it
with `sandbox.e2b.template` / `OMNIGENT_E2B_TEMPLATE`.

To run your own host image, build the `host` target of
[`deploy/docker/Dockerfile`](../docker/Dockerfile)
(`--platform linux/amd64`), push it anywhere E2B can pull from, and `FROM`
that ref in `e2b.Dockerfile` instead. Rebuild the template whenever the
host image changes (the CLI flow still overlays your *local* wheels on
top per-sandbox, so day-to-day code changes don't need a template
rebuild).

## CLI-launched sandboxes

Provision a sandbox and ship your local checkout into it:

```bash
omnigent sandbox create --provider e2b
```

This starts a sandbox from the `omnigent-host` template, builds wheels
from your local checkout, and overlays them on top — so the sandbox runs
*your* code, not whatever the template was built from. Then register it
as a host with your server:

```bash
omnigent sandbox connect --provider e2b \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the
connection open in your terminal — Ctrl-C tears it down (and kills the
remote process; E2B exposes a real kill handle). New sessions targeting
that host now run in the sandbox.

Running multiple sandboxes against one server? Pass a unique
`--host-name <label>` to each `connect` — the server keys hosts on
(owner, name), and sandboxes that share a hostname collide.

Sandboxes are disposable. When your code changes, create a new one — and
delete the old one (via the [dashboard](https://e2b.dev/dashboard) or
`e2b sandbox kill <id>`), though E2B also reaps it automatically at its
timeout.

To inject LLM/git credentials into a CLI-launched sandbox, set
`OMNIGENT_E2B_SANDBOX_ENV` in your shell to a comma-separated list of
variable names (e.g. `ANTHROPIC_API_KEY,GIT_TOKEN`) before running
`create` — the named variables are copied from your environment into the
sandbox at provision time.

> [!NOTE]
> E2B has no local→sandbox port forward (it exposes sandbox ports
> *outward* via public URLs only). The interactive in-sandbox
> `omnigent login` / App OAuth step is therefore skipped automatically
> (as on Modal / Daytona): use E2B with servers that don't require
> in-sandbox App auth, or authenticate via injected credentials (below).

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c
config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: e2b
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

`server_url` must be reachable *from E2B's cloud* — a public HTTPS URL,
not `localhost`. Sessions created with `host_type: "managed"` (the API
call or the Web UI's New Sandbox option) then run on a fresh E2B sandbox;
the create returns immediately and provisioning happens in the
background, exactly like the [Modal managed
flow](../modal/README.md#server-managed-sandboxes) — including repository
workspaces, the first-message rendezvous, and dead-sandbox relaunch.

Optional `e2b:` settings:

```yaml
sandbox:
  provider: e2b
  server_url: https://your-host
  e2b:
    template: omnigent-host          # E2B template NAME (default: omnigent-host)
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]
```

> [!NOTE]
> `sandbox.e2b.template` is an **E2B template name** (built above), not a
> registry image reference — the field that holds a `ghcr.io/...` ref on
> the other providers. Omit it to use the default `omnigent-host`
> template.

## Credentials for the sandbox (LLM keys, git tokens)

`sandbox.e2b.env` lists the **names** of variables to copy from the
**server's own environment** into every sandbox at provision time (passed
to `Sandbox.create(envs=…)`). Values never live in the config file — set
them where the server runs:

```bash
export OPENAI_API_KEY=sk-…       # on the server
export GIT_TOKEN=github_pat_…    # private-repo clone/fetch/push
```

```yaml
sandbox:
  provider: e2b
  server_url: https://your-host
  e2b:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

A listed name that is **not** set in the server's environment fails the
launch loudly (it would otherwise surface much later as an opaque harness
auth failure inside the sandbox).

Which variables to inject — providers, gateways, subscriptions, git — is
identical to Modal; see the [variable table and per-plan
recipes](../modal/README.md#llm-credentials-for-managed-sandboxes) and
[git credentials](../modal/README.md#git-credentials-private-repositories).
The in-sandbox host forwards the same standard set to its runners, and
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` (as an injected variable) names any
extras.

The same env-injection also carries **credentials for connecting to the
server itself**, for a host that authenticates its dial-back with user
credentials instead of a launch token. Managed launches never need this:
the server injects a per-launch host token automatically. But a
[CLI-launched](#cli-launched-sandboxes) host does when the server requires
authentication — name the keys (e.g. `DATABRICKS_HOST` +
`DATABRICKS_TOKEN`) in `OMNIGENT_E2B_SANDBOX_ENV` before `create`. See
[Connecting to an authenticated
server](../modal/README.md#connecting-to-an-authenticated-server) in the
Modal guide.

## Security considerations

- **Injected credentials reach E2B's control plane.** `sandbox.e2b.env`
  values are sent to E2B's API as literal sandbox env vars. Prefer
  **scoped, short-lived** credentials: a fine-grained PAT limited to the
  repos a session needs, a gateway token over a root provider key.
  (Modal's launcher attaches named Modal secrets, so its values stay in
  Modal's secret store — a stronger posture; same trade-off as the
  Daytona provider.)
- **All managed sandboxes share one E2B account + API key.** Cross-user
  isolation between Omnigent users rides on E2B's sandbox boundaries, and
  the shared key can enumerate and kill any user's sandbox. Scope the
  account to this workload.
- **The launch token's lifetime is ~25 h, derived from the *requested*
  lifetime.** E2B sandboxes share Modal's 24 h hard cap, so the per-launch
  host token outlives the sandbox by an hour to re-authenticate the tunnel
  across reconnects. The TTL is computed from `OMNIGENT_E2B_MAX_LIFETIME_S`
  (default 24 h) at server start, so it bounds the *longest possible*
  sandbox. On a capped account where `provision` clamps the sandbox shorter
  (e.g. Hobby's 1 h), the token over-covers the now-shorter sandbox — safe
  for re-auth, but a leaked token is replayable for the full window. To
  tighten this, **set `OMNIGENT_E2B_MAX_LIFETIME_S` to your account cap** so
  the token TTL tracks the granted lifetime (or set a shorter `token_ttl_s`
  on a directly-constructed `ManagedSandboxConfig`). A relaunch mints a
  fresh token.
- **Sandbox URLs are public by default.** E2B exposes sandbox ports via
  public `*.e2b.app` URLs; Omnigent never opens one (the host dials *out*
  to your server), but be aware nothing in a sandbox should bind a
  service expecting it to be private without E2B's access-token gating.

## Troubleshooting

- **"E2B sandbox creation failed: template '…' is unavailable"** — the
  host image was never built into an E2B template, or the name doesn't
  match. Run the [template build](#build-the-host-template-one-time) with
  `--name omnigent-host` (or set `sandbox.e2b.template` to your name).
- **"managed host did not come online within 120s"** — the sandbox
  couldn't dial back to `server_url`. Confirm it's a public HTTPS URL
  reachable from E2B's cloud (not `localhost`), and check
  `/tmp/omnigent-host.log` inside the sandbox.
- **Sandbox stops after ~1 hour** — you're on a Hobby account (1 h cap);
  `provision` auto-clamps to it (you'll see a one-line warning). Upgrade
  to Pro for the 24 h maximum, or expect the dead-sandbox relaunch path to
  re-provision on the next message.

## Lifecycle notes

- **Hard lifetime cap, no idle-stop disable.** `provision` requests
  `OMNIGENT_E2B_MAX_LIFETIME_S` (default the 24 h Pro maximum); E2B rejects
  a request above the account cap, so creation retries clamped to it (e.g.
  1 h on Hobby). `keep_alive` re-extends a live sandbox on reconnect, but
  there is no never-expire option — a managed session past the cap is
  replaced by the dead-sandbox relaunch path (same as Modal).
- **Templates, not registry images.** See
  [Build the host template](#build-the-host-template-one-time). Resources
  (vCPU / memory) are fixed when the template is built — pass
  `--cpu-count` / `--memory-mb` to `e2b template build` — not at sandbox
  create time.
- **Custom images** require rebuilding the template: `FROM` your image in
  `e2b.Dockerfile` and `e2b template build` it, then set
  `sandbox.e2b.template` / `OMNIGENT_E2B_TEMPLATE`.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `E2B_API_KEY` | CLI machine / server | E2B API credentials (required) |
| `OMNIGENT_E2B_TEMPLATE` | CLI machine / server | E2B template name to provision from (`sandbox.e2b.template` takes precedence; default `omnigent-host`) |
| `OMNIGENT_E2B_SANDBOX_ENV` | CLI machine / server | Comma-separated launcher-side env var names to inject (`sandbox.e2b.env` takes precedence for managed) |
| `OMNIGENT_E2B_MAX_LIFETIME_S` | CLI machine / server | Requested sandbox lifetime in seconds (default 24 h); creation auto-clamps to the account cap if exceeded |
