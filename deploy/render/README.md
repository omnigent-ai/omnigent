# Omnigent on Render

Deploy Omnigent to Render in one click. Render provisions the app and a
managed Postgres database, assigns an HTTPS URL on `*.onrender.com`, and
handles SSL automatically. No local tooling required.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/omnigent-ai/omnigent)

> **Note:** The button points at the public repo `github.com/omnigent-ai/omnigent`.
> It goes live once that repo **and** the `ghcr.io/omnigent-ai/omnigent-server`
> package are public; until then it only works if you connect Render to the
> (private) repo in the dashboard first.

## What gets provisioned

The `render.yaml` blueprint at the repo root defines:

- **omnigent** (Starter web service) — pulls the pre-built image
  `ghcr.io/omnigent-ai/omnigent-server:latest` (CI-built; ships the web UI
  bundle), served on `https://omnigent-<hash>.onrender.com`. While the GHCR
  package is private, add a Render registry credential and reference it from
  `render.yaml` (`image.creds`); once public, the pull is anonymous.
- **omnigent-db** (`basic-256mb` managed Postgres) — `DATABASE_URL` is injected
  into the service automatically
- **artifact-data** (10 GB persistent disk) — mounted at `/data` so server
  config, first-boot credentials, cookie secrets, and agent artifacts survive
  redeploys. Artifacts live under `/data/artifacts`.

## Quickstart (built-in accounts — the default)

The blueprint defaults to the built-in `accounts` auth provider: multi-user
out of the box, no external IdP, and **no env vars to fill in** — the server
mints its own cookie secret and auto-detects its public URL from Render.

1. Click the Deploy to Render button above → **Apply**. Wait ~3–5 min for the
   image pull + health check.
2. **Get the admin password:** open the service → **Logs** and find the
   first-boot block:
   ```
   ✓ Created initial admin account (accounts auth provider).
       password: <generated>
   ```
   (also written to `/data/admin-credentials` on the disk; printed once).
3. Open your `https://<service>.onrender.com` URL, log in as the admin, and
   invite teammates from **Members** in the web UI.

> To set a known admin password instead of the generated one, add
> `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` in the dashboard before first boot.

## Use your own IdP instead (OIDC)

Prefer to delegate login to GitHub / Google / Okta instead of built-in
accounts? Switch the provider after the initial deploy. HTTPS is provided
automatically by Render.

### GitHub OAuth (simplest to register)

1. Go to `github.com/settings/developers` → **New OAuth App**.
   - Homepage URL: `https://omnigent-<hash>.onrender.com`
   - Authorization callback URL:
     `https://omnigent-<hash>.onrender.com/auth/callback`
   - Click **Register application**, then **Generate a new client secret**.

2. In the Render dashboard, open the **omnigent** service → **Environment**
   and add / update these variables:

   | Variable | Value |
   |---|---|
   | `OMNIGENT_AUTH_PROVIDER` | `oidc` |
   | `OMNIGENT_OIDC_ISSUER` | `https://github.com` |
   | `OMNIGENT_OIDC_CLIENT_ID` | your GitHub OAuth client ID |
   | `OMNIGENT_OIDC_CLIENT_SECRET` | your GitHub OAuth client secret |
   | `OMNIGENT_OIDC_REDIRECT_URI` | `https://omnigent-<hash>.onrender.com/auth/callback` |

   Also add `OMNIGENT_OIDC_COOKIE_SECRET` = a 64-hex-char value from
   `openssl rand -hex 32` — OIDC mode requires it and validates it as hex.

3. Click **Save Changes**. Render redeploys automatically. Visit the URL —
   you'll be redirected to GitHub to log in.

### Google Workspace

| Variable | Value |
|---|---|
| `OMNIGENT_AUTH_PROVIDER` | `oidc` |
| `OMNIGENT_OIDC_ISSUER` | `https://accounts.google.com` |
| `OMNIGENT_OIDC_CLIENT_ID` | `…apps.googleusercontent.com` |
| `OMNIGENT_OIDC_CLIENT_SECRET` | your client secret |
| `OMNIGENT_OIDC_REDIRECT_URI` | `https://omnigent-<hash>.onrender.com/auth/callback` |
| `OMNIGENT_OIDC_ALLOWED_DOMAINS` | `example.com` (critical — see note below) |

> **Important:** Without `OMNIGENT_OIDC_ALLOWED_DOMAINS`, any Google account
> can log in when the OAuth consent screen is "External." Always restrict to
> your domain.

### Generic OIDC (Okta, Auth0, Keycloak, Entra ID)

Set `OMNIGENT_OIDC_ISSUER` to your IdP's base URL (the one that publishes
`/.well-known/openid-configuration`). The rest of the variables are the same
as above.

## Custom domain

In the Render dashboard, open the **omnigent** service → **Settings** →
**Custom Domains** → **Add Custom Domain**. Point your DNS CNAME at the
Render-assigned address. Render provisions a Let's Encrypt cert automatically.

Update `OMNIGENT_OIDC_REDIRECT_URI` to use the custom domain after DNS
propagates.

## Upgrading

Render redeploys automatically when a new commit lands on the connected branch
(if auto-deploy is enabled), or manually:

1. In the Render dashboard, open the **omnigent** service.
2. Click **Manual Deploy** → **Deploy latest commit**.

## Cost

Render: ~$7/month for the Starter web service + ~$6/month for the `basic-256mb`
managed Postgres. Total ~$13/month for a lightly loaded instance. Bump the
Postgres plan (`basic-1gb`, …) for more storage.

> **Note:** the web service needs a paid (Starter+) instance because of the
> persistent artifact disk, and Render's free Postgres plans expire — so a paid
> DB tier (`basic-256mb`) is the persistent default here.

> **Memory:** the Starter web service (512 MB) clears the server's ~512 MB–1 GB
> working set. Don't drop below it.

## Cheaper: SQLite on the disk (lite tier)

For a single-instance deploy you can skip the managed Postgres entirely and run
on **SQLite on the persistent disk** — it survives redeploys (the disk does) and
saves the ~$6/month DB cost. SQLite is a first-class backend; the tradeoff is
single-instance only (no horizontal scaling) and no managed backups, so keep
Postgres for production / multi-instance.

To use it, drop the `databases:` block from `render.yaml` and replace the
`DATABASE_URL` env var with a path on the disk:

```yaml
      - key: DATABASE_URL
        value: sqlite:////data/artifacts/chat.db
```

> **Or an external Neon Postgres.** You can point `DATABASE_URL` at a Neon
> database ([pg.new](https://pg.new)) instead of the managed Render one — e.g.
> to use Neon's free *persistent* tier rather than Render's paid DB. Tradeoff:
> you lose the integrated auto-provisioning (a separate signup + connection
> string) and add some cross-provider latency, so the managed Render Postgres
> stays the simpler default.
