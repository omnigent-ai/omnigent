# Deploying Omnigent

Omnigent ships several ways to deploy the server, organized by
target platform. Pick the one that matches your environment.

Deploying buys you a stable URL: sessions become reachable from any device,
including your phone (the web UI is built for mobile), and teammates can
join. The server is the coordination point; your code and model keys stay on
the machines that register as hosts (see [Execution model](#execution-model)).

## Deploy in one click

No local tooling needed. Pick a platform, click the button, and your
Omnigent server is live with HTTPS in a few minutes.

| Platform | Button | Docs |
|---|---|---|
| **Render** | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/omnigent-ai/omnigent) | [`render/README.md`](render/README.md) |
| **Railway** | *(button pending; see below)* | [`railway/README.md`](railway/README.md) |

<!-- TODO(oss-release): publish the Railway template at railway.com/new/template
     once the repo is public, then replace the Railway row above with:
     [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/<template-id>)
     Steps: railway.com/new/template → point at public repo → add Postgres plugin
     → publish → copy the deploy URL → update this file and deploy/railway/README.md. -->

Both provision a managed Postgres database automatically and default to the
built-in `accounts` auth provider, so a fresh deploy is multi-user with no
external IdP. First boot auto-creates an admin (password in the service
logs); invite teammates from the web UI. Prefer your own IdP? Switch to OIDC
after deploy by setting the `OMNIGENT_OIDC_*` vars (auth stays enabled; the
issuer is what flips the mode); see the platform README for both
walkthroughs.

**Three more platforms** are supported with a little more setup (not a single
button): **Fly.io** (`fly deploy`, or its web-UI Launch), **Hugging Face
Spaces** (a demo-grade Docker Space), and **Modal** (`modal deploy`, an
always-on web server with a durable artifact Volume). See the menu below.
Fly and HF Spaces can run on the **SQLite lite tier** with no database to
provision (see [Database: Postgres or SQLite](#database-postgres-or-sqlite));
Modal needs a bring-your-own Postgres.

---

```
deploy/
├── README.md          ← (this file) the menu
│
├── render/            ← Render 1-click deploy
│   └── README.md
│
├── railway/           ← Railway 1-click deploy
│   └── README.md
│
├── fly/               ← Fly.io (CLI `fly deploy`, or web-UI Launch)
│   ├── fly.toml
│   └── README.md
│
├── hf-spaces/         ← Hugging Face Spaces (demo-grade Docker Space)
│   ├── Dockerfile
│   └── README.md
│
├── modal/             ← Modal (`modal deploy`, always-on, durable Volume)
│   ├── modal_app.py
│   └── README.md
│
├── trycloudflare/     ← Cloudflare quick tunnel (public URL for a LOCAL server)
│   └── README.md
│
├── daytona/           ← Daytona sandbox-provider guide + the Cloudflare
│   ├── wrangler.toml     Worker egress relay for its free tier; NOT a
│   ├── src/index.js      server deploy target. See its README.md.
│   └── README.md
│
└── docker/            ← common Docker image + compose stack
    ├── Dockerfile         multi-stage slim image (node web build → python builder → runtime)
    ├── docker-compose.yaml   omnigent + postgres for any Docker host
    ├── entrypoint.py
    ├── .env.example
    ├── README.md
    └── SKILL.md
```

## Pick your target

| If you want to … | Use | Where to look |
|---|---|---|
| **Deploy from a browser (no local tools)** | **Render or Railway** | Buttons above: [Render](render/README.md) · [Railway](railway/README.md) |
| Try the server on your laptop | Docker compose | [`docker/README.md`](docker/README.md): `./bootstrap.sh` to mint the `.env` secrets, then `docker compose up -d` |
| Run on any host you already have (VPS, home server, on-prem) | Docker compose | [`docker/README.md`](docker/README.md): copy the compose stack, `./bootstrap.sh`, then `docker compose up -d` |
| Deploy to Fly.io | Fly | [`fly/README.md`](fly/README.md): `fly deploy`, SQLite on a volume |
| Deploy to Modal (durable artifact Volume) | Modal | [`modal/README.md`](modal/README.md): `modal deploy`, BYO Neon Postgres |
| Stand up a quick demo (no DB to provision) | HF Spaces | [`hf-spaces/README.md`](hf-spaces/README.md): Docker Space, SQLite |
| Share a server running on your **laptop**: demo it to teammates, or let remote runners & cloud sandboxes connect back to it (nothing to deploy) | Cloudflare quick tunnel | `cloudflared tunnel --url http://localhost:6767` |
| Cloud Run / Kubernetes / other | Docker image | [`docker/README.md`](docker/README.md), then point your platform at the image |

All deploy paths share the same image (`docker/Dockerfile`): a slim Python
container running the FastAPI / WebSocket coordinator, with Postgres or
SQLite as the datastore.

## Database: Postgres or SQLite

The server supports two database backends, both first-class (same schema, same
migrations; pick per `DATABASE_URL`):

- **Postgres**: the default and the production answer. Required for more than
  one server instance. **Managed and auto-provisioned on deploy** on Render and
  Railway. On platforms without a managed database (HF Spaces, Modal, or Fly
  if you want Postgres over volume-SQLite), bring your own. The quickest is
  **Neon**:
  create one at [pg.new](https://pg.new) and set the connection string as
  `DATABASE_URL`. Any `postgres://` / `postgresql://` URL works (pooled or
  direct); the entrypoint normalizes it to the psycopg3 dialect automatically.
- **SQLite**: a zero-dependency "lite tier" for demos and single-instance
  deploys, with no database to provision. The `.db` file lives on the
  platform's persistent disk/volume (Render disk, Fly volume, Railway volume)
  and survives restarts there; on Hugging Face free Spaces the disk is
  ephemeral, so SQLite data resets on restart, and on Modal the Volume's
  eventual-consistency semantics don't suit a live `.db` file, so skip the
  SQLite tier there. Set
  `DATABASE_URL=sqlite:////data/artifacts/chat.db`. Tradeoff: single instance
  only, no managed backups.

**Who provisions the database.** Render and Railway create the Postgres *as part
of the deploy* (one step; it's owned by your platform account). Platforms
without a managed DB don't: there you either run on SQLite (zero setup,
ephemeral on HF) or bring an owned Postgres like Neon (a one-time signup, then
persistent). A deploy can't auto-provision a *persistent* database for you;
persistence requires an owned account, and that's the one step that can't be
automated away.

**First boot against a remote Postgres is slow.** Migrations run over the
network on the first boot (~1 minute on Neon, vs near-instant for local SQLite);
subsequent boots are fast. Make sure the platform's healthcheck grace tolerates
it: Render and Railway do by default; on Fly, raise `grace_period` if you use a
remote DB.

**Memory floor:** the server's working set is ~512 MB–1 GB. Render Starter
(512 MB), Railway (usage-scaled), and HF Spaces clear it automatically; Fly's
256 MB default does not, so the Fly config pins a 1 GB machine, and the
Modal app pins `memory=1024` for the same reason.

## Execution model

Omnigent runs in two pieces that talk to each other over a
WebSocket tunnel:

- **Server**: the FastAPI app you deploy here. Handles HTTP / SSE
  routes, terminal-attach WebSockets, persistence, web UI.
- **Runner (host)**: a Python subprocess that runs on the **user's
  machine** (laptop, dev container, etc.). Dials in to the server
  via `WS /v1/runner/tunnel`, executes the LLM loop + tools locally,
  streams events back.

The deploy options here are all about the server. Runners aren't
deployed; every user launches one on their own machine with
`omnigent run …  --server <url>` or `omnigent claude  --server <url>`.

This separation is why the server image is small (no `tmux`, no
harness SDKs, no LLM API keys in the image) and why no agent code
runs inside it.

## Connect your laptop

Once the server is up, sign in from your machine. The token is reused by
`run`, `attach`, and `host`:

```bash
omnigent login https://your-host
```

`login` detects the server's auth mode automatically. Built-in accounts,
OIDC, header-auth proxies, and Databricks-hosted servers (a Databricks App
or a workspace API path) all work with the same command; for Databricks it
runs `databricks auth login` against the right workspace for you (requires
the `databricks` extra).

Then register the machine as a host, so sessions created in the web UI can
run on it:

```bash
omnigent host https://your-host
```

Or point a one-off run at the server directly:

```bash
omnigent run path/to/agent.yaml --server https://your-host
```

## Run hosts in cloud sandboxes

Don't want a laptop to be the host? Run the host in a cloud sandbox instead.

**From the CLI (Modal, Daytona, or Islo).** Install the provider extra when
needed (`pip install 'omnigent[modal]'` or `'omnigent[daytona]'`; Islo uses the
built-in HTTP client), authenticate (`modal token new`, `DAYTONA_API_KEY`, or
`ISLO_API_KEY`), then:

```bash
omnigent sandbox create --provider modal     # or --provider daytona / islo
omnigent sandbox connect --provider modal --sandbox-id <id> --server https://your-host
```

> [!NOTE]
> Modal caps sandbox lifetime at 24 hours. Re-run `create` + `connect` to
> roll the host onto a fresh sandbox. Daytona and Islo have no Omnigent-imposed
> lifetime cap; Daytona free-tier orgs restrict egress to an allowlist; see
> [`daytona/README.md`](daytona/README.md) for the relay workaround.

**Server-managed (Modal, Daytona, or Islo).** With *managed hosts*, creating a
session with `"host_type": "managed"` (e.g.
`POST /v1/sessions {"agent_id": ..., "host_type": "managed"}`) makes the
server provision a sandbox, start a host in it, and run the session there.
No laptop, no CLI steps per session; the sandbox is terminated when the
session is deleted. Configuration is a `sandbox:` section in the server
config (`omnigent server -c config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host        # public URL sandboxes dial back to
```

Modal credentials come from the server's environment (`MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET`, or a mounted `~/.modal.toml`), not the config file.
Daytona reads `DAYTONA_API_KEY`; Islo reads `ISLO_API_KEY` (and optional
`ISLO_BASE_URL`) from the server environment.
Each sandbox authenticates back with a server-minted, per-launch token, so
no user credentials ever enter the sandbox.

**The host image.** Sandboxes boot from the official prebaked host image
(`ghcr.io/omnigent-ai/omnigent-host:latest`, published by CI from the `host`
target of [`docker/Dockerfile`](docker/Dockerfile)), so the host starts in
seconds instead of installing Omnigent at boot. The image ships the
coding-harness CLIs (`claude`, `codex`, `pi`), so agents on any harness run
in the sandbox with nothing extra to install. To run sandboxes from your own
image instead (a fork, or extra tooling baked in), build the same `host`
target and point the config at it:

```bash
docker build -f docker/Dockerfile --target host \
  -t docker.io/<you>/omnigent-host:latest .
docker push docker.io/<you>/omnigent-host:latest
```

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    image: docker.io/<you>/omnigent-host:latest
```

For private registries, set `OMNIGENT_MODAL_REGISTRY_SECRET` on the server
to the name of a Modal secret holding `REGISTRY_USERNAME` /
`REGISTRY_PASSWORD`; for CLI-launched sandboxes, `OMNIGENT_MODAL_HOST_IMAGE`
(or `OMNIGENT_DAYTONA_HOST_IMAGE` / `OMNIGENT_ISLO_HOST_IMAGE`) overrides the
image ref.

**LLM credentials for managed sessions.** A fresh sandbox has no API keys.
Park your provider credentials in a [Modal secret](https://modal.com/secrets)
and list it in the config. Its env vars are injected into every managed
sandbox, and the in-sandbox host forwards the standard harness credential
vars (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `GEMINI_API_KEY`) to its runners:

```bash
modal secret create omnigent-llm \
  ANTHROPIC_API_KEY=sk-ant-… OPENAI_API_KEY=sk-…
```

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    secrets: [omnigent-llm]
```

For Daytona and Islo, list server environment variable names under
`sandbox.daytona.env` or `sandbox.islo.env`; the launcher copies the current
server env values into each sandbox:

```yaml
sandbox:
  provider: islo
  server_url: https://your-host
  islo:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

Using a **Claude subscription** instead of an API key? Run
`claude setup-token` on your own machine and store the resulting long-lived
token as `CLAUDE_CODE_OAUTH_TOKEN` in the secret. A **ChatGPT
Business/Enterprise plan** works the same way via a
[Codex access token](https://developers.openai.com/codex/enterprise/access-tokens)
stored as `CODEX_ACCESS_TOKEN`. For gateway setups or other env vars beyond
the standard set, add `OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2` to the
secret to name the extra vars the host should forward to runners.

**Private repositories.** Managed sessions can clone a repository as the
session workspace; for private ones, store an HTTPS token as `GIT_TOKEN` in
a Modal secret (GitLab: add `GIT_USERNAME=oauth2`). The host image's git
credential helper picks it up for the clone and for the agent's later
fetch/push.

The full Modal guide (CLI sandboxes, custom images, LLM and git credentials,
troubleshooting) lives at [`modal/README.md`](modal/README.md); the Daytona
guide lives at [`daytona/README.md`](daytona/README.md).

## Auth

Auth is driven by a single switch, `OMNIGENT_AUTH_ENABLED`. The framework
default (a bare local `omnigent server`) leaves it off: single-user
`header` mode, no login. The containerized deploys here (Docker / HF / Render /
Railway / Modal / Fly) set `OMNIGENT_AUTH_ENABLED=1` by default in their
entrypoints,
since a network-exposed instance should be authenticated. With the switch on,
the mode is chosen by your config: supply the `OMNIGENT_OIDC_*` vars and you
get `oidc`, otherwise you get the built-in `accounts` flow.
`OMNIGENT_AUTH_PROVIDER` is an explicit escape hatch that pins the mode and
overrides this auto-selection.

| Mode | When to use | What's needed |
|---|---|---|
| `accounts` (deploy default) | Standalone deploy, no external IdP: built-in username/password with first-user-is-admin bootstrap and UI-based invites. Opt in with `OMNIGENT_AUTH_ENABLED=1` (and no OIDC vars). | Set `OMNIGENT_ACCOUNTS_COOKIE_SECRET` (or let `bootstrap.sh` mint it) and `OMNIGENT_ACCOUNTS_BASE_URL` (public URL). On first boot, set the admin password via the web Create-admin form, the terminal prompt, or `--admin-password` / `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`. |
| `oidc` | Standalone deploy with your own IdP: server handles the full login flow | Set `OMNIGENT_AUTH_ENABLED=1` and the `OMNIGENT_OIDC_*` env vars; the presence of `OMNIGENT_OIDC_ISSUER` selects OIDC (or pin `OMNIGENT_AUTH_PROVIDER=oidc`). Requires HTTPS (the session cookie uses the `__Host-` prefix). |
| `header` | Behind an existing SSO proxy (oauth2-proxy, AWS ALB OIDC, Tailscale Funnel, …) that injects `X-Forwarded-Email` | The default when `OMNIGENT_AUTH_ENABLED` is off; or pin `OMNIGENT_AUTH_PROVIDER=header`. Proxy MUST strip any inbound copy of the header from clients. Missing headers are always rejected. |

### Single sign-on (OIDC)

The built-in `accounts` flow needs no setup beyond the deploy itself. To let
your team sign in with the accounts they already have (Google, GitHub, Okta,
Microsoft), point the server at your identity provider. In `docker/.env` (or
your platform's env settings):

```dotenv
# Auth is already on (OMNIGENT_AUTH_ENABLED=1) by default in the deploys here.
# Adding an OIDC issuer flips the mode to single sign-on. No extra flag.
OMNIGENT_OIDC_ISSUER=https://accounts.google.com     # or https://github.com / your Okta / Entra URL
OMNIGENT_DOMAIN=agents.yourcompany.com               # your server's domain
OMNIGENT_OIDC_CLIENT_ID=…
OMNIGENT_OIDC_CLIENT_SECRET=…
```

```bash
docker compose up -d        # restart to apply
```

Your team signs in with their existing accounts, and there are no passwords
for you to manage. Nothing else about the app changes.

> [!TIP]
> The only outside step is creating an app with your provider (e.g. Google
> Cloud Console, or GitHub → Settings → Developer settings) to get the client
> ID and secret. Set its **callback URL** to `https://<your-domain>/auth/callback`.

**Decide who's allowed in**, in your server config (`/data/config.yaml`):

```yaml
allowed_domains: [yourcompany.com]    # only your company's emails can sign in
admins: [you@yourcompany.com]         # who can manage members
```

> [!TIP]
> Need to let in one outsider, say a contractor on a personal account? Set
> `OMNIGENT_OIDC_ALLOW_INVITES=1` and send them a one-time invite link,
> instead of opening up the whole allowlist.

**Already have a team on built-in accounts?** One command brings everyone
across when you switch, so they keep their sessions and admin rights:

```bash
omnigent debug migrate-accounts-to-oidc <database-url> --domain yourcompany.com
```

For the provider-specific walkthroughs (GitHub OAuth, Google Workspace,
generic OIDC), see
[`docker/README.md#multi-user-mode-oidc`](docker/README.md#multi-user-mode-oidc).

### Header mode (X-Forwarded-Email)

> [!WARNING]
> Don't deploy a shared server in header-auth mode unless you run a trusted
> reverse proxy.

`header` mode (`OMNIGENT_AUTH_PROVIDER=header`) takes the caller's identity
from the `X-Forwarded-Email` request header. It exists for deployments that
sit behind an SSO proxy (oauth2-proxy, Cloudflare Access, an ALB/OIDC
listener, Databricks Apps) that authenticates the user and injects that
header on every request.

In header mode **the server trusts whatever that header says**. If no proxy
sets it, requests are rejected (`401`) rather than silently sharing one
identity. But a *misconfigured* proxy is still dangerous: if the proxy
doesn't **strip** any client-supplied `X-Forwarded-Email` before forwarding,
anyone can impersonate anyone by sending the header themselves. Getting this
wrong exposes every user's sessions, conversation history, tool output, and
files to every other caller.

**For almost everyone, use built-in `accounts` (the default in these
deploys) or `oidc`**; both authenticate users at the server with no proxy to
get right. Only choose `header` when you already operate a proxy you trust
to set and sanitize the identity header, and read
[`docker/README.md#header-proxy-mode-for-deploys-behind-an-existing-sso-proxy`](docker/README.md#header-proxy-mode-for-deploys-behind-an-existing-sso-proxy)
first.

## Adding a new deploy target

Drop a new subdirectory under `deploy/<target>/` with a `README.md`
and `SKILL.md`. If the new target uses the existing Docker image,
your work is mostly platform-specific glue (a `fly.toml`, a Cloud
Run service.yaml, a Helm chart, an HF Spaces config) plus a README
that explains how to point that platform at `docker/Dockerfile`.

Update this top-level README with a row in the table above.
