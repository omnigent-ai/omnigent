# Omnigent on Cloudflare (Containers + D1 + R2)

Run the Omnigent server on **Cloudflare Containers**, with **D1** as the
database and **R2** as the durable artifact store. This is the serverless,
scale-to-zero option: no VM or Postgres to manage, a public `*.workers.dev`
URL (or your domain), and the container sleeps when idle.

> [!NOTE]
> This is **not** the same as [`deploy/trycloudflare/`](../trycloudflare/),
> which is a quick tunnel that exposes a server running on **your laptop**.
> Here the server itself runs **on Cloudflare**.

> [!NOTE]
> This path uses a small SQLAlchemy dialect shim (`sitecustomize.py`) because
> Cloudflare D1 isn't yet first-class in Omnigent. It works end to end — it's how
> this directory was validated — and the normal on-boot migrations run unmodified
> (no schema-bootstrap step). See [What's still rough](#whats-still-rough) for the
> one upstream change that would remove the shim. The R2 artifact store, by
> contrast, already uses a first-class backend (`S3ArtifactStore`) added alongside
> this directory.

## How it works

```
        HTTPS / WebSocket
browser ───────────────►  Worker (src/index.js)
                              │   getContainer("singleton").fetch(req)
                              ▼
                          Container  ──►  the omnigent server (port 8000)
                          (1 instance)        │            │
                          DATABASE_URL ───────┘            │  S3 API (boto3)
                          cloudflare_d1://…                ▼
                                 │                  OMNIGENT_ARTIFACT_URI
                                 ▼                  s3://omnigent-artifacts
                          Cloudflare D1                    │
                          (SQLite, the DB)                 ▼
                                                    Cloudflare R2
                                                    (artifact store)
```

- **Worker** — a thin front that proxies every request to **one** container
  instance (Omnigent keeps an in-memory runner registry, so it's single-replica).
- **Container** — the official `ghcr.io/omnigent-ai/omnigent-server` image plus
  the D1 SQLAlchemy dialect, a shim that re-registers it as a proper SQLite
  dialect, and `boto3` (this directory's `Dockerfile`).
- **D1** is the database. The server reaches it through the
  `sqlalchemy-cloudflare-d1` dialect, which speaks D1's HTTP API — so
  `DATABASE_URL` is `cloudflare_d1://<account>:<api-token>@<database-id>`.
- **R2** is the artifact store. Cloudflare container disk is **ephemeral**, so
  artifacts (agent bundles, user files) go to R2 over its **S3 API** via
  Omnigent's native `S3ArtifactStore`, selected with
  `OMNIGENT_ARTIFACT_URI=s3://<bucket>`. No FUSE mount, no sidecar.

## What's in here

| File | Purpose |
|---|---|
| `Dockerfile` | derived image: server + D1 dialect + shim + boto3 |
| `sitecustomize.py` | shim re-registering `cloudflare_d1` as a SQLite dialect (auto-loaded) |
| `src/index.js` | the Worker that proxies to the container |
| `wrangler.jsonc` | Worker + Container + Durable Object config |
| `package.json` | `wrangler` + `@cloudflare/containers` |

## Prerequisites

- A Cloudflare account on the **Workers Paid** plan (~$5/mo) — Containers
  require it.
- **Docker** running locally (`wrangler deploy` builds the image).
- **Node** (for `wrangler`).
- `wrangler login` (or a `CLOUDFLARE_API_TOKEN`).

```bash
cd deploy/cloudflare
npm install
npx wrangler login
```

## Deploy

### 1. Create the D1 database

```bash
npx wrangler d1 create omnigent
# note the "database_id" it prints — call it <DATABASE_ID>
```

### 2. Create the R2 bucket

```bash
npx wrangler r2 bucket create omnigent-artifacts
```

### 3. A D1 API token (for `DATABASE_URL`)

The dialect authenticates to D1's REST API with a Cloudflare **API token**.
Create one at **dash.cloudflare.com → My Profile → API Tokens → Create Token →
Custom**, with permission **Account → D1 → Edit**. Your `DATABASE_URL` is then:

```
cloudflare_d1://<ACCOUNT_ID>:<D1_API_TOKEN>@<DATABASE_ID>
```

### 4. R2 S3 credentials (for the artifact store)

The artifact store uses R2's **S3 API**, which needs an Access Key ID + Secret
Access Key. Create them at **dash.cloudflare.com → R2 → Manage R2 API Tokens →
Create API Token → Object Read & Write**. It shows an **Access Key ID** and
**Secret Access Key** once — save both.

<details>
<summary>Alternative: derive S3 keys from an existing API token</summary>

Any API token with R2 permissions can be used as S3 credentials without minting
a separate R2 token ([docs](https://developers.cloudflare.com/r2/api/tokens/)):
**Access Key ID** = the token's *id*, **Secret Access Key** = `sha256(token value)`.

```bash
python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "<TOKEN_VALUE>"
```
</details>

### 5. Configure and set secrets

In `wrangler.jsonc`, set `AWS_ENDPOINT_URL_S3` to your account's R2 endpoint
(`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`). Then set the four secrets:

```bash
# DATABASE_URL — the cloudflare_d1:// string from step 3
npx wrangler secret put DATABASE_URL

# Session cookie secret — any 64-hex string
openssl rand -hex 32 | npx wrangler secret put OMNIGENT_ACCOUNTS_COOKIE_SECRET

# R2 S3 credentials from step 4
npx wrangler secret put AWS_ACCESS_KEY_ID
npx wrangler secret put AWS_SECRET_ACCESS_KEY
```

### 6. Deploy

```bash
npx wrangler deploy
# -> https://omnigent.<your-subdomain>.workers.dev
```

The container cold-starts on the first request (~10s), then stays warm:

```bash
curl https://omnigent.<your-subdomain>.workers.dev/health   # {"status":"ok"}
```

On a brand-new D1, the **first** boot runs all migrations before the server
starts listening (~1 minute against D1's REST API), so the first few requests
may return a 5xx while it migrates — just retry. Later boots are fast.

### 7. First admin + connect a host

Open the URL and the Setup screen claims the first admin (username + password).
Then connect a machine to actually run agents (the server is just the control
plane):

```bash
omnigent login https://omnigent.<your-subdomain>.workers.dev
omnigent host  --server https://omnigent.<your-subdomain>.workers.dev
```

## Verifying durability

The point of R2 is that state survives the ephemeral container. To prove it,
note your data, force a fresh container (`npx wrangler deploy` again, or let it
idle to sleep), and confirm it's still there — agents still load, sessions still
exist. The database lives in D1 and the artifacts in R2; the container holds
nothing durable.

## What's still rough

This deployment leans on one D1-specific workaround:

**The D1 dialect shim** (`sitecustomize.py`). The third-party
`sqlalchemy-cloudflare-d1` dialect subclasses the generic `DefaultDialect` and
hand-reimplements SQLite's SQL compilation and reflection incompletely. The shim
re-registers `cloudflare_d1` as a proper `SQLiteDialect` subclass — keeping only
the HTTP transport and D1 type processors — so DDL and reflection come from
SQLite and the **normal on-boot Alembic migrations run unmodified** (no
schema-bootstrap step). Upstream, the same wins come from fixing the dialect's
compilation and reflection directly — the composite-primary-key half is filed as
[CollierKing/sqlalchemy-cloudflare-d1#26](https://github.com/CollierKing/sqlalchemy-cloudflare-d1/pull/26);
with the dialect's reflection also complete, this shim would drop to a few lines
(an Alembic impl registration plus the "D1 has no `temp` schema" overrides).

The R2 artifact store has **no** such workaround — it uses the native
`S3ArtifactStore` backend (selected by `OMNIGENT_ARTIFACT_URI`), so the same
backend works for AWS S3, MinIO, etc. too.

Known runtime limitations:

- **Single replica only.** `max_instances: 1` — the runner registry is
  in-memory. Don't raise it.
- **Cold starts.** At `instances: 0` the container sleeps and the next request
  pays a boot cold start (~10s on `basic`). Bump `instance_type` to `standard`
  for more vCPU if that matters.
- **Image requirement.** The S3 artifact backend ships in the
  `deploy/docker/entrypoint.py` change alongside this directory; the published
  `omnigent-server` image must include it (build from this branch until it's
  released).

## Cost

Workers Paid is ~$5/mo and includes a container allowance that a small,
mostly-idle server fits inside. D1 and R2 both have free tiers comfortably
above what a single server uses. Past the allowance, containers bill per second
of actual run time — so scale-to-zero keeps an idle deployment cheap.
