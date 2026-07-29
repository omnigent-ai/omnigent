# Omnigent on Railway

Deploy Omnigent to Railway. Railway pulls the pre-built image, runs it next to
a managed Postgres, and serves it over HTTPS on `*.up.railway.app`.

> **Railway is not yet a true one-click.** Unlike Render's `render.yaml` (fully
> declarative — Postgres, port, and env all wired automatically), a bare
> `railway.toml` leaves several things to wire by hand (steps below). A real
> one-click experience needs a **published Railway template** that pre-wires the
> Postgres reference, `HOST`, and target port — tracked as a follow-up. Until
> then, use the manual steps here. (Render is the smoother path today.)

<!-- TODO(oss-release): publish a Railway template (pre-wiring Postgres + HOST +
     port) and add the button:
     [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/<template-id>) -->

## What gets provisioned

- **omnigent** — web service that pulls `ghcr.io/omnigent-ai/omnigent-server`
  via `deploy/docker/Dockerfile.prebuilt`, served on `https://<project>.up.railway.app`.
- **Postgres** — Railway-managed PostgreSQL plugin you add to the project.
  Railway links its `DATABASE_URL` into the app as a reference to the database
  instance's variable (largely automatic), but the value can lag on the first
  deploy — see step 2.

Artifact storage uses the container's local filesystem by default (ephemeral
across redeploys). For persistence, add a Railway Volume mounted at
`/data/artifacts`.

> **Optional: external Neon Postgres.** Instead of the Railway plugin, you can
> point `DATABASE_URL` at a Neon database ([pg.new](https://pg.new)) — e.g. for
> Neon's serverless scale-to-zero or branching. Tradeoff: you lose the
> integrated provisioning (a separate signup + connection string) and add some
> cross-provider latency, so the Railway plugin stays the simpler default.

## Setup (built-in accounts — the default)

Defaults to the `accounts` auth provider: multi-user, no external IdP. The
steps below are validated end-to-end:

1. **Deploy from the repo** — New Project → Deploy from GitHub repo → this repo.
   Railway reads `railway.toml` and pulls the image. **Add a Postgres plugin**
   to the project.
2. **Database** — Railway links the Postgres `DATABASE_URL` into the app as a
   reference to the db instance's variable (largely automatic when you add the
   plugin). If the first deploy errors with `DATABASE_URL is required`, the
   reference value simply hadn't propagated yet — **redeploy** and it resolves.
   (To confirm, the app service should have a `DATABASE_URL` variable
   referencing the Postgres service, e.g. `${{Postgres.DATABASE_URL}}`.)
3. **Create the first admin.** No credentials are auto-generated. The
   first-boot **Deploy logs** print a "No admin yet" line pointing at your
   `*.up.railway.app` URL (printed once; idempotent — later boots don't
   reprint). Open that URL and use the web Create-admin form to pick your own
   username + password.
4. Log in with the admin you just created, invite teammates from **Members**.

> **`HOST` is handled automatically.** Railway injects `HOST=[::]`, which a
> socket bind can't use and which Railway's IPv4 edge can't reach; the
> entrypoint detects Railway and coerces it to `0.0.0.0`, so no manual `HOST`
> variable is needed. If the generated domain returns "Application failed to
> respond," Railway's port auto-detect picked the wrong port — open
> Settings → Networking and set the domain's target port to the `PORT` Railway
> injected (shown in the boot log as `Uvicorn running on …:<port>`).

> The cookie secret is auto-minted and `OMNIGENT_ACCOUNTS_BASE_URL` is
> auto-detected from `RAILWAY_PUBLIC_DOMAIN`, so those don't need setting. To
> pin a known admin password, set `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`
> before first boot.

> **Security note for public deployments:** `POST /auth/setup` is
> unauthenticated while no password-bearing account exists, so an instance
> exposed before you reach the Create-admin form can be claimed by the first
> visitor. Pre-seed `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`, or complete setup
> promptly after the deploy goes live.

## Use your own IdP instead (OIDC)

Prefer GitHub / Google / Okta login over built-in accounts? Switch the provider
in the service Variables. OIDC requires HTTPS — Railway provides it
automatically on `*.up.railway.app`. If you set a custom domain, point it at
your project before completing these steps.

### GitHub OAuth (simplest to register)

1. Go to `github.com/settings/developers` → **New OAuth App**.
   - Homepage URL: `https://<project>.up.railway.app`
   - Authorization callback URL: `https://<project>.up.railway.app/auth/callback`
   - Click **Register application**, then **Generate a new client secret**.

2. In your Railway project, open the **omnigent** service → **Variables**
   and add:

   | Variable | Value |
   |---|---|
   | `OMNIGENT_AUTH_PROVIDER` | `oidc` |
   | `OMNIGENT_OIDC_ISSUER` | `https://github.com` |
   | `OMNIGENT_OIDC_CLIENT_ID` | your GitHub OAuth client ID |
   | `OMNIGENT_OIDC_CLIENT_SECRET` | your GitHub OAuth client secret |
   | `OMNIGENT_OIDC_REDIRECT_URI` | `https://<project>.up.railway.app/auth/callback` |
   | `OMNIGENT_OIDC_COOKIE_SECRET` | output of `openssl rand -hex 32` |

3. Railway redeploys automatically. Visit the URL — you'll be redirected to
   GitHub to log in.

### Google Workspace

| Variable | Value |
|---|---|
| `OMNIGENT_AUTH_PROVIDER` | `oidc` |
| `OMNIGENT_OIDC_ISSUER` | `https://accounts.google.com` |
| `OMNIGENT_OIDC_CLIENT_ID` | `…apps.googleusercontent.com` |
| `OMNIGENT_OIDC_CLIENT_SECRET` | your client secret |
| `OMNIGENT_OIDC_REDIRECT_URI` | `https://<project>.up.railway.app/auth/callback` |
| `OMNIGENT_OIDC_COOKIE_SECRET` | output of `openssl rand -hex 32` |
| `OMNIGENT_OIDC_ALLOWED_DOMAINS` | `example.com` (critical — see note below) |

> **Important:** Without `OMNIGENT_OIDC_ALLOWED_DOMAINS`, any Google account
> can log in when the OAuth consent screen is "External." Always restrict to
> your domain.

### Generic OIDC (Okta, Auth0, Keycloak, Entra ID)

Set `OMNIGENT_OIDC_ISSUER` to your IdP's base URL (the one that publishes
`/.well-known/openid-configuration`). The rest of the variables are the same
as above.

## Custom domain

In your Railway project, open **Settings** → **Domains** → **Add domain**.
Point your DNS A/AAAA record at the Railway-assigned address. Railway
provisions a Let's Encrypt cert automatically.

Update `OMNIGENT_OIDC_REDIRECT_URI` to use the custom domain after DNS
propagates.

## Upgrading

Railway redeploys automatically when a new image tag is pushed to GHCR
(if you've configured a webhook) or on demand:

1. In the Railway dashboard, open the **omnigent** service.
2. Click **Deploy** → **Latest** to pull the newest `:latest` image.

## Cost

Railway Hobby plan: ~$5/month base + per-minute CPU/memory usage. A lightly
loaded Omnigent instance (few concurrent users) typically stays under
$10–15/month total including the Postgres plugin.

## Publishing the template

One-time setup done by the repo owner after the repository is public:

1. Go to `railway.com/new/template` and click **Create template**.
2. Point it at `github.com/omnigent-ai/omnigent`.
3. Select the **Postgres** plugin.
4. Pre-fill default env vars with descriptions for the optional OIDC fields.
5. Click **Publish**. Copy the generated deploy URL and update the badge at the
   top of this file and in `deploy/README.md`.
