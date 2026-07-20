# Omnigent on Tenki

[Tenki](https://tenki.cloud) sandboxes give you disposable Linux microVMs
for running Omnigent hosts, two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a
  sandbox from your terminal, builds and ships wheels from your local
  checkout into it, and registers it as a host with your server.
- **Server-managed**: the server provisions a sandbox automatically when
  a session is created with `"host_type": "managed"` and terminates it
  when the session is deleted.

> [!IMPORTANT]
> **Tenki boots from a *prepared* image, not a stock one.** A Tenki session
> starts from a registry image reference (`<workspace>/<name>:tag`), and the
> Omnigent host plus its dependencies must already be installed in that image:
> the CLI flow only overlays your *local* wheels with `pip install --no-deps`,
> and the server-managed flow ships no wheels at all (it runs `omnigent host`
> directly). So you build the host into a **Tenki template** once (below) and
> point the launcher at it. There is **no default image** — registry refs are
> workspace-scoped, so the launcher fails fast until you name one. This
> directory is **not** a server deploy target.

## Prerequisites

```bash
pip install 'omnigent[tenki]'                     # installs the tenki-sandbox SDK extra
curl -fsSL https://tenki.cloud/install.sh | bash  # the Tenki CLI, for building the template
```

Create an API key at [tenki.cloud](https://tenki.cloud) and make it available
where the launcher runs — your shell for the CLI flow, the **server** process
for managed sandboxes:

```bash
export TENKI_API_KEY=tk_…
tenki login                    # one-time, authenticates the Tenki CLI too
```

Optionally set `TENKI_API_ENDPOINT` to target a non-default Tenki API endpoint
(the SDK also honors it; `sandbox.tenki.base_url` takes precedence for managed).

> [!IMPORTANT]
> **Sessions are created in a project.** The Tenki service rejects `create` with
> `project_id is required` unless your API key is itself project-scoped. A key
> from interactive `tenki login` is **not** project-scoped, so set the project id
> via `sandbox.tenki.project` (managed) / `OMNIGENT_TENKI_PROJECT` (CLI) — and
> optionally the workspace via `sandbox.tenki.workspace` /
> `OMNIGENT_TENKI_WORKSPACE`. Find both ids with `tenki status`.

> [!NOTE]
> **No forced lifetime cap or idle auto-pause by default.** Omnigent sets no
> session `max_duration`, and Tenki's default idle timeout is `0` — auto-pause is
> disabled (verified on Tenki CLI v0.18.1) — so a sandbox runs until it is
> terminated (managed-session teardown, or your own `tenki sandbox terminate`).
> `keep_alive` extends a live session on reconnect. Only if your *workspace* sets
> a non-zero default idle timeout could a long-quiet host be paused; Omnigent does
> not resume Tenki sandboxes in place, so it would then be replaced by the
> dead-sandbox relaunch path on the next message. Keep idle-pause disabled (or use
> `--sticky`-style always-on sessions) to avoid that.

_The CLI commands below were verified against **Tenki CLI v0.18.1**; re-check
`tenki … -h` if your version differs._

## Build the host template (one time)

Tenki builds a template by running a **setup script** on a base image
(`--base-image`, default `sandbox`) and capturing the result as a reusable
snapshot, then publishing it to your workspace registry. The setup script must
install the Omnigent host and its dependencies (and the coding-harness CLIs your
agents use) into a Python environment whose `pip` is first on `PATH`, so the CLI
flow's later `pip install --no-deps` wheel overlay lands in the same
environment. The default `sandbox` base already ships `python3`, `git`, and
`curl` (runs as user `tenki`, `$HOME=/home/tenki`), so the script mainly adds
Omnigent; adapt it to your base image.

```bash
# 1. Define the template — prints the template id (adapt the setup script)
tenki sandbox template create \
  --name omnigent-host \
  --setup-script '
    set -eux
    python3 -m venv /opt/venv
    /opt/venv/bin/pip install --upgrade pip
    /opt/venv/bin/pip install omnigent
    # Put the venv first on PATH for every session started from this template.
    echo "export PATH=/opt/venv/bin:\$PATH" >> /etc/profile.d/omnigent.sh
  '

# 2. Build it — waits until READY by default (no --wait flag); add
#    --wait-durable to also block on the snapshot upload
tenki sandbox template build <template-id>

# 3. Publish it to your workspace registry under an explicit tag.
#    NOTE: the `latest` tag is reserved on Tenki — pick your own (e.g. v1).
tenki sandbox registry publish \
  --image <workspace>/omnigent-host:v1 \
  --from-template <template-id> \
  --visibility private
```

> [!NOTE]
> The `<workspace>` in a registry ref is a **workspace slug**, not the workspace
> UUID and not a name with spaces. A freshly-created default workspace may not
> have a usable slug — set one in the Tenki dashboard first, or `registry
> publish` fails with `invalid workspace slug`.

Point the launcher at the published reference with `sandbox.tenki.image` /
`OMNIGENT_TENKI_IMAGE` (there is no default — the ref is workspace-scoped).
Rebuild + republish the template whenever the baked host version changes (the
CLI flow still overlays your *local* wheels on top per-sandbox, so day-to-day
code changes don't need a template rebuild). Find your workspace id with
`tenki status`.

## CLI-launched sandboxes

Provision a sandbox and ship wheels built from your local checkout into it:

```bash
export OMNIGENT_TENKI_IMAGE=<workspace>/omnigent-host:v1
omnigent sandbox create --provider tenki
```

This starts a sandbox from your prepared image, builds wheels from your local
checkout, and overlays them on top — so the sandbox runs *your* code, not
whatever the template was built from. Then register it as a host with your
server:

```bash
omnigent sandbox connect --provider tenki \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the connection open
in your terminal — Ctrl-C tears it down (and kills the remote process; Tenki
exposes a real kill handle). New sessions targeting that host now run in the
sandbox.

Running multiple sandboxes against one server? Pass a unique `--host-name
<label>` to each `connect` — the server keys hosts on (owner, name), and
sandboxes that share a hostname collide.

To inject LLM/git credentials into a CLI-launched sandbox, set
`OMNIGENT_TENKI_SANDBOX_ENV` in your shell to a comma-separated list of variable
names (e.g. `ANTHROPIC_API_KEY,GIT_TOKEN`) before running `create` — the named
variables are copied from your environment into the sandbox at provision time.

> [!NOTE]
> Tenki has no local→sandbox port forward (it exposes sandbox ports *outward*
> via public URLs only). The interactive in-sandbox `omnigent login` / App OAuth
> step is therefore skipped automatically (as on Modal / Islo / E2B): use Tenki
> with servers that don't require in-sandbox App auth, or authenticate via
> injected credentials (below).

> [!IMPORTANT]
> **CLI-bootstrap wheel-shipping needs `/tmp` inside the image's workdir.** Tenki's
> file API is *workdir-scoped* — it rejects paths outside the session workdir with
> `path outside workdir`. The bootstrap ships the wheel tarball to
> `/tmp/oa-wheels.tgz`, so your prepared host image must use a workdir that
> contains `/tmp` (e.g. `WORKDIR /`) for `omnigent sandbox create` to overlay your
> local wheels. If it doesn't, use **server-managed** hosts instead — they boot
> the baked image and ship no wheels, so the workdir scope never applies.

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c config.yaml`,
or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: tenki
  server_url: https://your-host              # public URL sandboxes dial back to
  tenki:
    image: <workspace>/omnigent-host:v1  # prepared registry image (required)
    project: <project-id>                # required unless the key is project-scoped
```

`server_url` must be reachable *from Tenki's cloud* — a public HTTPS URL, not
`localhost`. Sessions created with `host_type: "managed"` (the API call or the
Web UI's New Sandbox option) then run on a fresh Tenki sandbox; the create
returns immediately and provisioning happens in the background, exactly like the
[Modal managed flow](../modal/README.md#server-managed-sandboxes) — including
repository workspaces, the first-message rendezvous, and dead-sandbox relaunch.
Provisioning enables outbound networking (so the host dials back and clones) and
leaves inbound disabled.

Optional `tenki:` settings:

```yaml
sandbox:
  provider: tenki
  server_url: https://your-host
  tenki:
    image: <workspace>/omnigent-host:v1
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]
    project: <project-id>                     # required unless the key is project-scoped
    workspace: <workspace-id>                 # optional
    base_url: https://api.tenki.cloud        # optional API endpoint override
    vcpus: 4                                  # optional (1–16, default 2)
    memory_mb: 8192                           # optional (128–65536, default 4096)
    disk_gb: 40                               # optional (5–100)
```

## Credentials for the sandbox (LLM keys, git tokens)

`sandbox.tenki.env` lists the **names** of variables to copy from the
**server's own environment** into every sandbox at provision time (passed to
`Client.create(env=…)`). Values never live in the config file — set them where
the server runs:

```bash
export OPENAI_API_KEY=sk-…       # on the server
export GIT_TOKEN=github_pat_…    # private-repo clone/fetch/push
```

```yaml
sandbox:
  provider: tenki
  server_url: https://your-host
  tenki:
    image: <workspace>/omnigent-host:v1
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

A listed name that is **not** set in the server's environment fails the launch
loudly (it would otherwise surface much later as an opaque harness auth failure
inside the sandbox).

Which variables to inject — providers, gateways, subscriptions, git — is
identical to Modal; see the [variable table and per-plan
recipes](../modal/README.md#llm-credentials-for-managed-sandboxes) and [git
credentials](../modal/README.md#git-credentials-private-repositories). The
in-sandbox host forwards the same standard set to its runners, and
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` (as an injected variable) names any extras.

## Security considerations

- **Injected credentials reach Tenki's control plane.** `sandbox.tenki.env`
  values are sent to Tenki's API as literal sandbox env vars. Prefer **scoped,
  short-lived** credentials: a fine-grained PAT limited to the repos a session
  needs, a gateway token over a root provider key.
- **All managed sandboxes share one Tenki account + API key.** Cross-user
  isolation between Omnigent users rides on Tenki's sandbox boundaries, and the
  shared key can enumerate and terminate any user's sandbox. Scope the account
  to this workload.
- **The launch token's lifetime is 7 days (policy, not platform).** Omnigent
  sets no session `max_duration`, so the per-launch host token is bounded by a
  fixed 7-day window — long enough to re-authenticate the tunnel across
  reconnects while still expiring tokens of sandboxes nobody terminated. A
  relaunch mints a fresh token. Set a shorter `token_ttl_s` on a
  directly-constructed `ManagedSandboxConfig` to tighten it.
- **Sandbox URLs are public when exposed.** Tenki exposes sandbox ports via
  public URLs on request; Omnigent never opens one (the host dials *out* to your
  server), but nothing in a sandbox should bind a service expecting it to be
  private.

## Troubleshooting

- **"No Tenki host image configured"** — no image resolved. Build and publish
  the [host template](#build-the-host-template-one-time), then set
  `sandbox.tenki.image` (managed) or `OMNIGENT_TENKI_IMAGE` (CLI).
- **"Tenki sandbox creation failed: …"** — the image ref is unknown/unpublished,
  the resources exceed Tenki's bounds (vCPU 1–16, memory 128–65536 MB, disk
  5–100 GB), or the account quota is exhausted. The message carries Tenki's
  reason verbatim.
- **"managed host did not come online within 120s"** — the sandbox couldn't dial
  back to `server_url`. Confirm it's a public HTTPS URL reachable from Tenki's
  cloud (not `localhost`), and check `/tmp/omnigent-host.log` inside the sandbox.
- **Session stops while idle** — your workspace may apply a default idle timeout.
  Omnigent does not resume Tenki sandboxes in place; the dead-sandbox relaunch
  path re-provisions on the next message.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `TENKI_API_KEY` | CLI machine / server | Tenki API credentials (required; `TENKI_AUTH_TOKEN` also accepted) |
| `TENKI_API_ENDPOINT` | CLI machine / server | Tenki API endpoint override (`sandbox.tenki.base_url` takes precedence for managed) |
| `OMNIGENT_TENKI_IMAGE` | CLI machine / server | Prepared registry image reference to boot from (`sandbox.tenki.image` takes precedence; no default) |
| `OMNIGENT_TENKI_PROJECT` | CLI machine / server | Tenki project id sessions are created in (`sandbox.tenki.project` takes precedence; required unless the key is project-scoped) |
| `OMNIGENT_TENKI_WORKSPACE` | CLI machine / server | Tenki workspace id sessions are created in (`sandbox.tenki.workspace` takes precedence; optional) |
| `OMNIGENT_TENKI_SANDBOX_ENV` | CLI machine / server | Comma-separated launcher-side env var names to inject (`sandbox.tenki.env` takes precedence for managed) |
