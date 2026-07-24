# Omnigent on Fly.io

Deploy Omnigent to Fly.io. Fly pulls the prebuilt image, runs it next to a
persistent volume, and serves it over HTTPS on `*.fly.dev`.

> **Fly is CLI-first.** There's no embeddable one-click button like Render's;
> you deploy with `fly deploy` (or, with one extra config tweak, Fly's web-UI
> Launch — see below). Both are validated.

## What gets provisioned

- **omnigent** — a machine that pulls `ghcr.io/omnigent-ai/omnigent-server`,
  served on `https://<app>.fly.dev`.
- **artifact_data** — a persistent volume mounted at `/data/artifacts`, holding
  the artifact store, the minted cookie secret, and (by default) the SQLite
  database.

The default `fly.toml` uses **SQLite on the volume** — no separate database
app. That's persistent across restarts and fine for a single instance. For
multi-instance, point `DATABASE_URL` at a Postgres URL instead (see below).

## Deploy (CLI — the primary path)

Built-in `accounts` auth (multi-user, no external IdP) is the default.

```bash
# from the repo root
fly apps create <your-app>                                  # globally unique name
fly volumes create artifact_data --size 1 --region iad -a <your-app>   # match fly.toml region
fly deploy -c deploy/fly/fly.toml -a <your-app>
```

Then:

1. **Memory** — `fly.toml` pins a **1 GB** machine (`[[vm]] memory = "1gb"`).
   The server idles around ~275 MB RSS, so Fly's 256 MB default OOM-loops.
   Keep it at 1 GB (or `fly scale memory 1024 -a <your-app>` if you changed it).
2. **Create the first admin.** No credentials are auto-generated. First boot
   prints a "No admin yet" line pointing at your `*.fly.dev` URL:
   ```bash
   fly logs -a <your-app>
   ```
   Open `https://<your-app>.fly.dev` and use the web Create-admin form to pick
   your own username + password. For a headless deploy, pre-seed
   `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` (`fly secrets set …`) before first
   boot to create the admin directly instead.
3. Log in with the admin you just created. The cookie secret and base URL
   (`FLY_APP_NAME` -> `<app>.fly.dev`) are handled automatically.

> **Security note for public deployments:** `POST /auth/setup` is
> unauthenticated while no password-bearing account exists, so an instance
> exposed before you reach the Create-admin form can be claimed by the first
> visitor. Pre-seed `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`, or complete setup
> promptly after the deploy goes live.

## Deploy (Fly web-UI Launch)

Fly's web Launch *builds* an image and pushes it to Fly's own registry — it has
no "deploy this external image" mode, so the default `[build] image = ...`
404s there. To use the web UI, switch `fly.toml` to build the one-line shim:

```toml
[build]
  dockerfile = "deploy/docker/Dockerfile.prebuilt"
```

The shim is `FROM ghcr.io/omnigent-ai/omnigent-server` with nothing added, so
Fly **pulls the prebuilt image and re-tags it** — no source rebuild. Launch
still won't auto-create the `artifact_data` volume or bump memory, so create
the volume (above) and confirm 1 GB after Launch finishes.

## Use Postgres instead of SQLite

For multiple instances or managed backups, use Postgres instead of the volume
SQLite. Two options:

- **Fly Postgres:**
  ```bash
  fly postgres create
  fly postgres attach <pg-app-name> -a <your-app>    # sets DATABASE_URL as a secret
  ```
- **Neon (serverless Postgres):** create one at [pg.new](https://pg.new) (sign
  in to keep it), then `fly secrets set DATABASE_URL='postgres://...' -a <your-app>`.

Either way, remove the `DATABASE_URL = "sqlite:..."` line from `[env]` so the
attached/secret value wins. The entrypoint normalizes the `postgres://` URL
automatically.

> **Bump the healthcheck grace for a remote DB.** The first boot against an
> external Postgres (Neon) runs migrations over the network and takes ~1 minute;
> the volume-SQLite default is near-instant. If you switch to a remote DB, raise
> `grace_period` in the `[[http_service.checks]]` block (20s -> ~90s) so Fly
> doesn't kill the machine mid-migration on the first deploy.

## Use your own IdP instead (OIDC)

Switch the provider with `fly secrets set` (OIDC requires HTTPS, which Fly
provides on `*.fly.dev`):

```bash
fly secrets set \
  OMNIGENT_AUTH_PROVIDER=oidc \
  OMNIGENT_OIDC_ISSUER=https://github.com \
  OMNIGENT_OIDC_CLIENT_ID=<client-id> \
  OMNIGENT_OIDC_CLIENT_SECRET=<client-secret> \
  OMNIGENT_OIDC_REDIRECT_URI=https://<your-app>.fly.dev/auth/callback \
  OMNIGENT_OIDC_COOKIE_SECRET=$(openssl rand -hex 32) \
  -a <your-app>
```

For Google Workspace, also set `OMNIGENT_OIDC_ALLOWED_DOMAINS` to restrict
logins to your domain.

## Cost

A `shared-cpu-1x` 1 GB machine plus a 1 GB volume runs a few dollars a month
for a lightly loaded instance. Add a Postgres app only if you move off SQLite.
