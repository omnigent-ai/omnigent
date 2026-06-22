# Omnigent on Databricks Apps (Lakebase Postgres)

Run the Omnigent server as a [Databricks App](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/),
backed by a [Lakebase](https://docs.databricks.com/aws/en/oltp/) managed
Postgres database. The app runs the **same** FastAPI server every other
platform runs — `deploy/databricks/src/app.py` is a thin shim over the generic
entrypoint (`deploy/docker/entrypoint.py`); it only bridges the Databricks
runtime contract (port, Lakebase env vars, identity-aware-proxy auth) onto the
environment variables that entrypoint already speaks.

## Layout

| File | Purpose |
|---|---|
| `src/app.py` | Databricks Apps entrypoint. Bridges env, then reuses the generic entrypoint's `_resolve_config` / `build_app`. |
| `src/app.yaml` | Apps manifest — `command` + `env` (port, Lakebase instance, artifact dir, auth). |
| `src/requirements.txt` | App runtime deps. `deploy.py` repoints this at the locally built wheel. |
| `databricks.yml` | Asset Bundle (DAB): the app resource + the Lakebase database binding. |
| `deploy.py` | Builds the wheel, stages it + the vendored entrypoint into `src/`, runs `databricks bundle deploy`. |
| `grant_sp_perms.py` | Registers the app's service principal as a Lakebase Postgres role. |

## Quick start

```bash
# 0. Prereqs: Databricks CLI v0.295+ authenticated to your workspace, a
#    Lakebase database instance (default name: omnigent-db), and a Unity
#    Catalog Volume for artifacts (see "Artifact store" below).

# 1. Edit src/app.yaml: set OMNIGENT_LAKEBASE_INSTANCE and ARTIFACT_DIR.
#    Edit databricks.yml var defaults to match (lakebase_instance/database).

# 2. Build the wheel + deploy the bundle (creates/updates the app).
python deploy/databricks/deploy.py -t dev

# 3. Start the app.
databricks bundle run omnigent_server -t dev

# 4. Grant the app's service principal access to Lakebase (one-time; the SP
#    only exists after step 2 creates the app). --superuser the first time so
#    the boot migration can create the schema.
python deploy/databricks/grant_sp_perms.py \
  --app-name omnigent-server --instance omnigent-db --superuser

# 5. Get the URL.
databricks apps get omnigent-server
```

> The omnigent wheel must bundle the prebuilt web-UI assets
> (`omnigent/server/static/web-ui`) — a source-only checkout can't regenerate
> them. CI builds that bundle; build the wheel from a checkout that has it, or
> the deployed app serves no UI.

## Lakebase connection & OAuth token rotation

Lakebase does **not** use a static password. It authenticates with a
short-lived **OAuth token** (~1 hour TTL, rotated) used as the Postgres
*password*. A long-lived server that baked one token into its connection URL
would lose the database the moment that token expired — pooled connections
would start failing mid-flight. So Omnigent mints a **fresh token per new
connection** instead.

### URI format

The app composes (in `configure_databricks_env`) a **password-less** URL from
the Lakebase env vars Databricks injects (`PGHOST` / `PGUSER` / `PGDATABASE` /
`PGPORT`):

```
postgresql+psycopg://<sp-client-id>@<host>:5432/<database>?sslmode=require
```

- `postgresql+psycopg://` — the psycopg3 dialect SQLAlchemy needs.
- No password segment — it is injected per connection at connect time.
- `sslmode=require` — mandatory for Lakebase.
- `<sp-client-id>` — the app's service principal (the `PGUSER` Databricks injects).

### How the token path is enabled

The token path in `omnigent/db/utils.py` is **opt-in and fully backward
compatible** — a static SQLite or static-password Postgres URL behaves exactly
as before. It activates only when a token provider resolves, which happens when:

```
OMNIGENT_LAKEBASE_INSTANCE=<lakebase-instance-name>
```

is set (in `src/app.yaml`). When active, the engine:

1. Registers a SQLAlchemy `do_connect` listener that calls
   `WorkspaceClient().database.generate_database_credential(...)` and sets the
   freshly minted token as the connection password — **once per new physical
   connection** (not per pooled checkout).
2. Lowers `pool_recycle` from 30 min to **600 s**, so a connection (and its
   token) is rebuilt comfortably before the ~1 h token lifetime lapses, even if
   it sits idle in the pool across a rotation.

The token is minted with the app's ambient service-principal credentials
(`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`, auto-injected by the Apps
runtime). For a non-default credential flow, inject your own provider via
`omnigent.db.utils.set_lakebase_token_provider(...)` instead of the env var.

`grant_sp_perms.py` ensures the SP exists as a Postgres role on the instance so
both the token mint and the connection succeed.

## Single-replica constraint

**The app must run as a single replica.** Omnigent's runner registry lives in
**server process memory**: a runner's WebSocket tunnel is held by one process,
and the routing table that maps sessions to runners is not shared across
processes. Spreading traffic over multiple instances would route requests to
instances that don't hold the runner, and scaling to zero would tear down live
tunnels.

Databricks Apps run a single container per app by default. **Do not** raise the
app's compute instance count (`compute_max_instances`) above 1, and do not put
the app behind anything that fans out to multiple instances. This is the same
constraint the Modal deploy documents (`min_containers=1`/`max_containers=1`).

## Artifact store (persistence)

The Apps container filesystem is **ephemeral** — a redeploy wipes it. Uploaded
agent bundles and the build cache must live somewhere durable, so point
`ARTIFACT_DIR` at a **Unity Catalog Volume** (FUSE-mounted, durable, shared):

```yaml
# src/app.yaml
env:
  - name: ARTIFACT_DIR
    value: "/Volumes/<catalog>/<schema>/<volume>"
```

Create the volume once (`CREATE VOLUME <catalog>.<schema>.omnigent_artifacts;`)
and grant the app's service principal `READ VOLUME` / `WRITE VOLUME`.

Alternatively, use an S3/Cloudflare-R2 object store by setting
`OMNIGENT_ARTIFACT_URI=s3://bucket/prefix` (the generic entrypoint selects the
remote `S3ArtifactStore`); supply the bucket credentials via a secret resource.
With a single replica a Volume is sufficient; the object store is there if you
prefer cloud-native durability.

## Auth

Databricks fronts the app with an identity-aware proxy that forwards the
authenticated user in the `X-Forwarded-Email` header. The app defaults to
`OMNIGENT_AUTH_PROVIDER=header`, which reads exactly that header — so users are
authenticated by Databricks with no extra config. To use your own OIDC IdP
instead, set `OMNIGENT_AUTH_PROVIDER=oidc` and the `OMNIGENT_OIDC_*` vars (map
secrets with `valueFrom` in `app.yaml`).

## Secrets

Map [Databricks secrets](https://docs.databricks.com/aws/en/security/secrets/)
to env vars with `valueFrom` (instead of `value`) in `src/app.yaml`, and
declare the matching `secret` resource in `databricks.yml` so the app's SP can
read the scope/key. The Lakebase password is **not** a secret here — it is
minted at runtime — so the only secrets you typically need are OIDC client
secrets (if you opt out of the Databricks proxy) or object-store credentials.

## Relationship to `deploy/modal/`

This directory is additive — the Modal deploy glue remains for users on Modal.
Both run the identical server image/entrypoint; only the platform wiring (and,
here, the Lakebase token path) differs.
