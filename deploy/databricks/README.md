# Deploying Omnigent on Databricks Apps

This directory deploys the Omnigent server to
[Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
via [Databricks Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/):

- **Lakebase** (managed PostgreSQL) — the database for every store.
- **UC Volumes** — the artifact store for agent bundles and executor
  storage snapshots.

> **Most Databricks users want the managed offering instead.**
> [Omnigent on Databricks](https://docs.databricks.com/aws/en/omnigent/)
> (Beta) runs the server for you, wired to workspace identity,
> Foundation Models, AI Gateway, and MLflow Tracing out of the box.
> Enable the **Omnigent** preview in your workspace settings and follow
> the quickstart there. Use this directory only when you need to
> self-manage the deployment: the managed service is not in your region
> yet, or you need control it does not expose today (custom YAML
> policies, bring-your-own provider API keys, custom egress controls).

The orchestrator at `deploy.py` builds the wheels, generates an app
`pyproject.toml` + `uv.lock`, and then runs
`databricks bundle deploy` + `bundle run` against the bundle config
in `databricks.yml`. App config (Lakebase, UC volume) lives
declaratively in `databricks.yml` — adding or removing a resource is
a YAML edit, not a Python SDK call.

Runs unchanged from a laptop. Re-runnable; every step is idempotent.

## Prerequisites

1. A Databricks workspace with Databricks Apps, Lakebase, and UC
   Volumes enabled.
2. The [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install.md)
   installed and authenticated. Either a CLI profile
   (`DATABRICKS_CONFIG_PROFILE=<profile>`) or env-based auth
   (`DATABRICKS_HOST` + `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET`).
3. The repo's local venv with the `databricks` extra:
   `uv sync --extra databricks` (use `uv`, not global pip).
4. Permissions to create or use:
   - a Lakebase project (one per app — do not share with other apps);
   - a UC volume whose parent catalog/schema can grant access to the
     app service principal;
   - (optional) Databricks secrets for LLM API keys.

Set your workspace URL in `databricks.yml` under
`targets.prod.workspace.host` (it ships as a `https://example.databricks.com`
placeholder; DAB reads `workspace.host` before resolving variables, so it
must be a literal).

## One-time bootstrap

### 1. Lakebase project (one per app — never share)

Reusing a shared autoscaling project causes the migrate-on-boot hook
to fail with "permission denied for table agents" because the tables
are owned by whoever ran migrations first. Always start fresh:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Project

wc = WorkspaceClient(profile="<your-profile>")
wc.postgres.create_project(project=Project(), project_id="omnigent")

branch = "projects/omnigent/branches/production"
endpoint = f"{branch}/endpoints/primary"

import time
for _ in range(120):
    ep = wc.postgres.get_endpoint(name=endpoint)
    if ep.status and ep.status.current_state == "ACTIVE":
        break
    time.sleep(5)
else:
    raise TimeoutError(endpoint)

database = next(iter(wc.postgres.list_databases(parent=branch)))
print("database resource path:", database.name)
```

### 2. UC Volumes

```sql
CREATE SCHEMA IF NOT EXISTS main.omnigent;
CREATE VOLUME IF NOT EXISTS main.omnigent.artifacts;
```

The `artifacts` volume is referenced declaratively in `databricks.yml`
(the app resource) via `--var volume_name=…`.

### 3. First deploy — creates the app and its service principal

Run the [Deploy](#deploy) command once. The first
`databricks bundle deploy` creates the app and provisions its service
principal (SP). This first pass will **not** pass its `/health` check
yet: the SP has no Lakebase schema grants, so the migrate-on-boot hook
fails with `permission denied for schema public`. That's expected —
the next step grants those privileges.

### 4. Grant the app SP Lakebase privileges

Now that the app (and its SP) exist, grant the SP the public schema
privileges Alembic needs, then re-run the deploy:

```bash
python deploy/databricks/grant_sp_perms.py \
    --app-name omnigent \
    --lakebase-endpoint projects/omnigent/branches/production/endpoints/primary \
    --database databricks_postgres \
    --profile <your-profile>
```

> [!NOTE]
> Lakebase uses two spellings for the same database. The **resource
> path** uses a hyphenated slug — `…/databases/databricks-postgres`
> (what `deploy.py --lakebase-database` and `databricks.yml` want) —
> while the underlying **PostgreSQL database name** uses an underscore,
> `databricks_postgres` (what `grant_sp_perms.py --database` and the
> app's `PGDATABASE` use). Pass each form where shown.

After this one-time grant, re-running the deploy boots the app cleanly
and `/health` returns 200. Subsequent redeploys are a single
`deploy.py` invocation.

## Deploy

```bash
uv run python deploy/databricks/deploy.py \
    --app-name omnigent \
    --profile <your-profile> \
    --lakebase-branch projects/omnigent/branches/production \
    --lakebase-database projects/omnigent/branches/production/databases/databricks-postgres \
    --volume-name main.omnigent.artifacts
```

The script builds wheels, classifies them by size, copies wheels into
`src/`, regenerates `src/pyproject.toml` and `src/uv.lock`, runs
`databricks bundle deploy --target prod`, runs
`databricks bundle run omnigent --target prod`, and polls `/health`
with backoff until 200.

All Omnigent wheels must fit under the Databricks Apps source
snapshot limit (10 MB). If a wheel exceeds it, rebuild with
`--skip-web-ui` or reduce the wheel size; uv lockfiles cannot point at
UC Volume wheel paths because `uv lock` validates path sources locally.

Re-running is safe — every step is idempotent.

> [!TIP]
> To lock against a private PyPI mirror or proxy instead of public
> PyPI, set `UV_INDEX_URL` before running `deploy.py`.

## Smoke check

`deploy.py` polls `/health` automatically. To check other endpoints:

```bash
TOKEN="$(databricks auth token <your-profile> --output json \
    | python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"

curl --http1.1 -fsS \
    -H "Authorization: Bearer ${TOKEN}" \
    https://<app>.databricksapps.com/health
```

## How it works

### Authentication

The app runs as a Databricks service principal. Credentials are
managed automatically:

- **Lakebase** — OAuth tokens generated via
  `WorkspaceClient.postgres.generate_database_credential()`, injected
  into every new SQLAlchemy connection via a class-level
  `do_connect` event hook in `src/app.py`.
- **UC Volumes** — Workspace credentials used by the Databricks SDK
  (ambient in Apps).
- **TUI / API access** — Browser-based OAuth using the
  `databricks-cli` OIDC client with PKCE.

The Databricks Apps proxy injects `X-Forwarded-Email` on every
request, so the app pins `OMNIGENT_AUTH_PROVIDER=header` (see
`src/app.py`).

> [!IMPORTANT]
> Header auth trusts the `X-Forwarded-Email` header verbatim. This is
> safe **only** because the Databricks Apps platform terminates auth at
> its proxy, strips any client-supplied copy of the header, and the app
> port is never reachable except through that proxy. Don't expose the
> app process directly (e.g. a port forward or alternate ingress that
> bypasses the proxy): a caller who can set the header themselves could
> then impersonate any user. If you front the app with anything other
> than the standard Apps proxy, ensure it sanitizes the header too.

### Token lifecycle

Lakebase OAuth tokens expire after 60 minutes. The SQLAlchemy
connection pool recycles connections every 5 minutes by default
(configurable via `AP_POOL_RECYCLE_SECONDS`), ensuring fresh tokens
on new connections.

### Storage

| Component | Backend | Purpose |
|---|---|---|
| Agent specs, tasks, conversations | Lakebase (PostgreSQL) | Durable metadata |
| Agent bundles, executor snapshots | UC Volumes | Binary blob storage |
| DBOS workflow state | Lakebase (same DB) | Workflow recovery |
| Executor working dirs | Local ephemeral disk | Cache (restored from UC Volumes) |

## Configuration reference

Environment variables read by `src/app.py`:

| Variable | Source | Description |
|---|---|---|
| `PGHOST` | Databricks runtime | Lakebase hostname |
| `PGPORT` | Databricks runtime | Lakebase port (default 5432) |
| `PGDATABASE` | Databricks runtime | Lakebase database name |
| `PGUSER` | Databricks runtime | Lakebase user (service principal) |
| `PGSSLMODE` | Databricks runtime | SSL mode (default `require`) |
| `AP_LAKEBASE_ENDPOINT` | app resource `valueFrom: postgres` | Lakebase endpoint for token generation |
| `AP_ARTIFACT_VOLUME_PATH` | app resource `valueFrom: artifact_volume` | UC Volume path for artifacts |
| `DATABRICKS_APP_PORT` | Databricks runtime | App port (default 8000) |
| `AP_POOL_RECYCLE_SECONDS` | Optional | Connection pool recycle interval (default 300) |

## Multi-app safety — one bundle, many apps

The same bundle directory can deploy many apps (one per `--app-name`).
Terraform can only delete or replace what is tracked in the state it
loads, so the blast radius of a deploy is exactly that state file.

- **Remote state is per app.** `targets.<t>.workspace.root_path` ends
  in `${var.app_name}`, so `--app-name X` reads and writes state only
  under `.bundle/omnigent/X`. A deploy of X cannot mutate app Y.
- **The app resource's `name` is `${var.app_name}`.** If the loaded
  state tracks app X but you pass `app_name=Y`, terraform sees a name
  change and plans a **destroy of X + create of Y**. Never bind the
  bundle resource to one app and then deploy with a different
  `--app-name`.
- The **local** cache at `deploy/databricks/.databricks/bundle/<target>/`
  is per-*target*. Before deploying a *different* app on a target
  you've used before, drop it: `rm -rf deploy/databricks/.databricks/bundle/<target>`
  (it's only a cache; the per-app remote state is the source of truth).

If a `bundle deploy` plan ever shows a delete or replace of a
`databricks_app`, abort and re-check the bind and `--app-name` —
routine redeploys only ever update in place.

## Common deploy modes

```bash
# Iterate without rebuilding wheels (reuses dist/; useful when you only
# changed app.py / app.yaml). Skips the clean-tree check.
uv run python deploy/databricks/deploy.py --skip-build --allow-dirty ...

# API-only deploy (drops the SPA from the main wheel).
uv run python deploy/databricks/deploy.py --skip-web-ui ...
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Deploy refuses: "working tree has uncommitted changes" / "HEAD is not at origin/main" | Clean-tree assertion | Commit/stash, `git checkout main && git pull`, or pass `--allow-dirty` |
| `bundle deploy` fails: "Resource already managed by Terraform" | App already bound to another bundle directory | Run from that directory, or unbind: `databricks bundle deployment unbind omnigent` |
| `bundle deploy` fails: "An app with the same name already exists" | App exists but isn't bound to this bundle (or a stale per-target local cache from a *different* app made `deploy.py` skip the bind) | `rm -rf deploy/databricks/.databricks/bundle/<target>`, then bind: `databricks bundle deployment bind omnigent <app-name> --target <target> --auto-approve --var ...` |
| App fails "Error installing packages"; `/logz` shows "Ignoring existing lockfile due to … exclude newer …" then a PyPI fetch timeout | The Apps runtime pins a global uv `exclude-newer` cutoff; a lock generated without the matching option is re-resolved in-container, where PyPI is unreachable | Read the cutoff from `/logz` ("change of exclude newer timestamp from X to Y") and redeploy with `UV_EXCLUDE_NEWER=<cutoff>` in the environment |
| `permission denied for table agents` | Lakebase tables owned by wrong user | Connect as the owner and `DROP TABLE … CASCADE`; redeploy |
| `schema "dbos" already exists` | Same for the DBOS schema | `DROP SCHEMA dbos CASCADE` and redeploy |
| `permission denied for schema public` | App SP missing schema grants | Run `grant_sp_perms.py` (one-time) |
| `Field 'spec.role' cannot be empty` | Lakebase requires explicit role for extra databases | Use the project's default database; don't create extras |
| Deploy refuses because a wheel is over 10 MB | uv app payload requires local wheel path sources | Rebuild with `--skip-web-ui` or reduce wheel size |
| App starts cleanly but the first agent request 403s on the artifact volume | App SP has `WRITE_VOLUME` on the leaf but no `USE_CATALOG` / `USE_SCHEMA` on the parents | `deploy.py` grants both automatically — for a fresh catalog, redeploy or grant manually via `databricks grants update` |

## Files in this directory

| File | Purpose |
|---|---|
| `deploy.py` | Orchestrator. Single entry point. |
| `databricks.yml` | DAB bundle config. Declares the app + its resources. |
| `build.sh` | Cleans static, builds the web UI, builds three wheels. |
| `grant_sp_perms.py` | One-time Lakebase `public` schema grant for the app SP. |
| `src/app.py` | The app process. SQLAlchemy `do_connect` token hook + Alembic-on-boot + uvicorn. |
| `src/app.yaml` | App startup config — command + env-var wiring. |
| `src/pyproject.toml` / `src/uv.lock` | Regenerated per deploy; not committed (they pin the per-deploy wheel version). |

## See also

- [`databricks.yml`](./databricks.yml) — DAB bundle config.
- [Databricks Apps docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/).
- [Databricks Asset Bundles docs](https://docs.databricks.com/aws/en/dev-tools/bundles/).
