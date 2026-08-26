# DigitalOcean deployment

This deployment keeps GitHub as the source of truth, runs the Omnigent server
on DigitalOcean App Platform in Singapore, and provisions coding hosts as
replaceable `sgp1` Droplets backed by persistent Block Storage.

```text
GitHub fork (main, deploy_on_push)
        ↓
DigitalOcean App Platform (SGP)
        ├── Omnigent server
        ├── Managed PostgreSQL
        └── Spaces (durable artifacts)
                 ↓ DigitalOcean API
        ephemeral Droplet + persistent Volume (sgp1)
                 ↓
        official omnigent-host container
```

The App Platform filesystem is deliberately treated as ephemeral. The startup
wrapper generates `/tmp/.../config.yaml` from environment variables on every
boot. PostgreSQL holds application state, Spaces holds uploaded artifacts, and
each managed host's Block Storage volume holds its workspace and host-local
Omnigent credentials.

## One-time setup

1. Fork `omnigent-ai/omnigent` on GitHub and keep the deployment branch named
   `main`.
2. In DigitalOcean, create a production Managed PostgreSQL cluster in
   Singapore. Copy its TLS connection string for `DATABASE_URL`.
3. Create a private Spaces bucket in `sgp1` and a restricted Spaces access key.
   This is the durable artifact backend; omitting it makes uploads ephemeral.
4. Create a scoped DigitalOcean API token for the server. It needs read/write
   access for Droplets, Block Storage and volume actions. In the custom-scope
   picker, grant `droplet:create/read/update/delete`,
   `block_storage:create/read/delete`, `block_storage_action:create/read`, and
   `tag:create/read`, plus the prerequisite read scopes DigitalOcean lists for
   actions, regions, sizes, images, snapshots, and VPCs. Do not reuse a
   personal full-access token when a custom-scoped token is available.
5. Edit [`.do/app.yaml`](../../.do/app.yaml) before importing it and replace the
   Spaces bucket. The GitHub source is this fork and `OMNIGENT_PUBLIC_URL` uses
   App Platform's `${APP_URL}` binding. Secret declarations intentionally have
   no values; enter them in DigitalOcean, never in the committed file.
6. In **Apps → Create App**, connect GitHub, select this fork and `main`, and
   grant DigitalOcean repository access. Import `.do/app.yaml` or reproduce its
   settings in the control panel. Fill each encrypted secret variable before
   the first successful start, and confirm **Autodeploy** is enabled.
7. Keep the App Platform region as `sgp`. Keep PostgreSQL, Spaces, Droplets and
   volumes in Singapore (`sgp1`) for the lowest practical latency from Bangkok.
8. Deploy once, open the App URL, and create the initial accounts-mode admin
   using `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`. Rotate or remove that bootstrap
   password after the first admin exists.

After setup, the deployment path is simply:

```text
pull request → repository CI → merge to main → App Platform auto-deploy
```

GitHub Actions validate the fork but never create DigitalOcean resources and do
not perform deployment.

## Required App Platform variables

| Variable | Secret | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | yes | Managed PostgreSQL TLS connection string |
| `DIGITALOCEAN_TOKEN` | yes | Server-only Droplet/Volume lifecycle token |
| `OMNIGENT_PUBLIC_URL` | no | Exact public `https://...ondigitalocean.app` URL |
| `OMNIGENT_AUTH_PROVIDER=accounts` | no | Keeps the public server authenticated |
| `OMNIGENT_ACCOUNTS_COOKIE_SECRET` | yes | Stable accounts session-cookie secret |
| `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` | yes | Initial admin bootstrap; rotate/remove later |
| `OMNIGENT_ARTIFACT_URI` | no | `s3://bucket/prefix` for durable Spaces artifacts |
| `AWS_ENDPOINT_URL_S3` | no | `https://sgp1.digitaloceanspaces.com` |
| `AWS_ACCESS_KEY_ID` | yes | Restricted Spaces key |
| `AWS_SECRET_ACCESS_KEY` | yes | Restricted Spaces secret |
| `OMNIGENT_FEATURES=harness_install` | no | Enables secure host credential setup in the UI |

Generate the cookie secret as at least 32 random bytes encoded as hex, for
example `python -c 'import secrets; print(secrets.token_hex(32))'`. Use the TLS
PostgreSQL connection string DigitalOcean provides; do not remove its SSL
settings.

The App Spec also sets these configurable host defaults:

```text
DIGITALOCEAN_REGION=sgp1
DIGITALOCEAN_DROPLET_SIZE=s-4vcpu-8gb
DIGITALOCEAN_DROPLET_IMAGE=ubuntu-24-04-x64
DIGITALOCEAN_WORKSPACE_SIZE_GB=100
DIGITALOCEAN_WORKSPACE_MOUNT_PATH=/workspace
OMNIGENT_DIGITALOCEAN_HOST_IMAGE=ghcr.io/omnigent-ai/omnigent-host:latest
```

`s-4vcpu-8gb` is DigitalOcean's current 4-vCPU/8-GiB Basic size slug. Verify
availability in `sgp1` with `doctl compute size list` or `GET /v2/sizes` before
first production use because regional capacity changes. `image` may also be a
private snapshot ID; snapshots are optional and never used by suspend/resume.

Equivalent generated server configuration is:

```yaml
sandbox:
  provider: digitalocean
  server_url: https://your-app.ondigitalocean.app
  digitalocean:
    region: sgp1
    size: s-4vcpu-8gb
    image: ubuntu-24-04-x64
    host_image: ghcr.io/omnigent-ai/omnigent-host:latest
    workspace:
      size_gb: 100
      mount_path: /workspace
```

## Managed-host lifecycle

Create performs these bounded, idempotent steps:

1. Find the exact owned volume or create a preformatted ext4 volume.
2. Create a tagged Ubuntu Droplet without SSH keys, inbound application ports,
   backups, IPv6, or the web-console agent.
3. Wait for the Droplet, attach the same-region volume, and wait for the action.
4. Cloud-init waits for that block device, verifies it is ext4 without ever
   reformatting it, mounts it, installs Docker, and runs the official host image.
5. The host connects outbound over TLS and registers with its scoped launch
   token. No inbound port is needed.

Lifecycle endpoints are owner-scoped:

```text
POST   /v1/hosts/{host_id}/suspend
POST   /v1/hosts/{host_id}/resume
DELETE /v1/hosts/{host_id}
```

Suspend asks the guest OS to shut down (allowing Docker and the filesystem to
flush), waits for it to turn off, and falls back to a hard power-off only after
a bounded graceful timeout. It then detaches the volume, deletes the Droplet,
revokes that generation's launch token, and preserves the volume. Resume validates the owned volume,
creates a new Droplet, attaches it, mints a fresh launch token, and restarts the
host under the same Omnigent host/workspace identity. Sending a message to an
offline resumable session also uses Omnigent's existing wake path.

Permanent deletion deletes a confidently tagged Droplet and the exact matching
owned volume, then removes the host row. Deleting a managed session already
uses this permanent teardown path. A volume with missing/mismatched ownership
tags, name, region, or filesystem is refused rather than guessed at.

While suspended, the coding Droplet is gone. App Platform, Managed PostgreSQL,
Spaces, and the Block Storage volume continue to incur their normal charges.

## Model and Git credentials

The DigitalOcean token is never injected into a Droplet. Model keys and Git
PATs are also excluded from cloud-init, Droplet names, tags, and API metadata.

For Claude, Codex, or OpenRouter, connect the host and use the Omnigent Web UI's
host setup dialog. The server forwards the credential once over the
authenticated TLS host tunnel; the host writes it to its credential store on
the persistent volume. For OpenRouter use:

```text
Codex/OpenAI-compatible base URL: https://openrouter.ai/api/v1
Claude/Anthropic-compatible base URL: https://openrouter.ai/api
```

Native subscription credentials such as `CLAUDE_CODE_OAUTH_TOKEN` or
`CODEX_ACCESS_TOKEN` can be configured through the same supported host setup
flow where offered. Direct API-key setups use `ANTHROPIC_AUTH_TOKEN` or
`OPENAI_API_KEY` semantics internally; raw values are not stored by the server.

For GitHub HTTPS access, start an empty managed workspace, open its terminal,
and store a narrowly scoped PAT on the persistent volume:

```bash
git config --global credential.helper store
printf 'protocol=https\nhost=github.com\nusername=x-access-token\npassword=YOUR_PAT\n\n' \
  | git credential approve
git clone https://github.com/OWNER/PRIVATE_REPO.git
```

The provider mounts both `.gitconfig` and `.git-credentials` from Block
Storage, so they survive suspend/resume. Prefer a fine-grained, repository-only
PAT. Creating a brand-new managed session directly from a private repository
URL is intentionally not supported in v1 because doing so before the host
connects would require putting the PAT in cloud-init. Public repository URLs
clone normally.

## Security model

- `DIGITALOCEAN_TOKEN` exists only in App Platform's encrypted runtime env and
  authorizes the server's API client. It is never sent to host compute.
- Model keys travel through the existing authenticated TLS host tunnel and are
  stored only in the host credential store on encrypted-at-rest Block Storage.
- A GitHub PAT is entered inside the connected host and stored on its volume;
  it is not known to App Platform or cloud-init.
- Cloud-init contains the raw Omnigent launch token because the host needs it
  for outbound registration and reconnect. It is scoped to one server-chosen
  host ID, stored only as a digest in PostgreSQL, expires after seven days, is
  rotated on resume, and is revoked on suspend/delete. Anyone with read access
  to Droplet user-data during that window can impersonate that one managed host,
  so DigitalOcean account access remains a trusted administrative boundary.
- Tags and names contain only non-secret stable workspace identifiers.
- API errors include the operation/resource/status but never request headers or
  bearer tokens. Rate limits and transient server errors use bounded backoff.

No Cloud Firewall is required for v1 because the Droplet exposes no application
port and receives no SSH key. If account policy requires one, attach a reusable
outbound-permissive/inbound-deny firewall to the `omnigent-managed` tag.

## Recovery and operations

Resource names and the paired `omnigent-managed`, `omnigent-workspace-*`, and
`omnigent-sandbox-*` tags allow a fresh server process to rediscover the exact
volume and current compute generation. Partial attach failures delete the new
Droplet but preserve the volume. Cleanup failures remain visible in server logs
and never broaden into account-wide tag deletion.

Provider-managed idle timing cannot be wired cleanly into the current generic
activity lifecycle, so `idle_timeout_minutes` is intentionally deferred. Use
the explicit suspend endpoint for v1; resume and on-message wake are supported.

## Updating from upstream

The `Upstream sync` GitHub workflow runs every Monday at 10:17 Bangkok time
and can also be started with **Run workflow**. It merges upstream `main` into
the rolling `automation/upstream-sync` branch and opens or refreshes a pull
request; it never pushes directly to `main`. A merge conflict fails the job and
leaves both branches untouched.

For the sync PR to trigger its own CI, create a fine-grained GitHub token with
**Contents: read/write** and **Pull requests: read/write**, save it as the
`UPSTREAM_SYNC_TOKEN` Actions secret, and grant it access only to this fork.
Without that secret, the workflow falls back to `GITHUB_TOKEN`; enable
**Settings → Actions → General → Allow GitHub Actions to create and approve
pull requests**. Pull-request checks created by the default token require a
maintainer to select **Approve workflows to run** in the PR before they start.

`OMNIGENT_UPSTREAM_REPOSITORY` and `OMNIGENT_UPSTREAM_BRANCH` repository
variables can override the default `omnigent-ai/omnigent` and `main` source.
The manual equivalent remains:

```bash
git remote add upstream https://github.com/omnigent-ai/omnigent.git  # once
git fetch upstream
git checkout main
git rebase upstream/main
git push --force-with-lease origin main
```

The final push triggers App Platform's native GitHub autodeploy. No deployment
token is needed in GitHub Actions.

## Local validation

```bash
uv sync --extra all --group dev
uv run pytest tests/deploy/test_digitalocean_entrypoint.py \
  tests/onboarding/sandboxes/test_digitalocean.py \
  tests/onboarding/sandboxes/test_registry.py \
  tests/server/test_managed_hosts.py
uv run pre-commit run --all-files
uv run pyrefly check
docker build --build-arg OMNIGENT_EXTRAS=s3 \
  -f deploy/docker/Dockerfile -t omnigent-digitalocean:test .
git diff --check
```

These commands use mocks and build images only. They do not create paid
DigitalOcean resources.
