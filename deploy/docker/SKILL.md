---
name: deploy-docker-compose
description: Run the Omnigent server as a Docker compose stack (server + Postgres) on any Docker host — your laptop, a VPS, EC2 by hand, or as the base layer of any container-platform deploy. Invoke when the user wants to build the image, bring up the compose stack, debug the stack on a host they already have, or extend the stack for a new platform.
---

# Run Omnigent as a Docker compose stack

The `Dockerfile` here is the single image used by every non-Databricks
deploy path. It bundles the FastAPI server + a pre-built web SPA
into a slim Python runtime. The compose file pairs it with Postgres
and exposes the server on port 8000.

The image is "external runner only" — it does NOT include `tmux`,
the harness SDKs, or anything that would let it execute agent code
in-process. Runners live on user machines and dial in via the
WebSocket tunnel. This keeps the image small (~250 MB), the security
boundary clean (server doesn't execute user code), and the deploy
shape consistent across hosts.

The same Dockerfile also has a `host` target — the prebaked Omnigent
HOST image (`omnigent-host`) that remote sandboxes boot from
(`omnigent sandbox create --provider modal`, server-launched managed
hosts). It is the inverse profile: full omnigent install plus git +
tmux, no SPA, no psycopg, no server entrypoint. Both images are
published by the same workflows with the same `:sha-<short>` /
`:latest` / `:vX.Y.Z` tag scheme. See the "Host image" section in
`README.md` here.

## TL;DR — bring it up

```bash
cd deploy/docker
cp .env.example .env             # edit POSTGRES_PASSWORD at minimum
docker compose up -d --build
docker compose logs -f omnigent   # Ctrl-C when you see "Uvicorn running"
```

Server is on http://localhost:8000.

## Files

| | |
|---|---|
| `Dockerfile` | Multi-stage build with two final targets. `web-builder` (node:20) runs `npm install && npm run build` on `web/`. `builder` (python:3.12) installs omnigent into `/opt/venv`; `server-builder` overlays the SPA bundle from `web-builder` and adds psycopg. The default target (`runtime`) copies the venv + `/build/` from `server-builder` and runs `entrypoint.py`. `--target host` builds the host image instead (from `builder`: omnigent + git/tmux, no SPA/psycopg/entrypoint). |
| `Dockerfile.dockerignore` | BuildKit-aware exclude. Trims `deploy/databricks/`, `deploy/aws/`, tests, dev tooling — keeps the build context small. |
| `entrypoint.py` | Server process entrypoint. Reads `DATABASE_URL`, runs Alembic migrations, builds the SQLAlchemy stores, calls `create_app()`, runs uvicorn. Single source of truth for what env vars the container respects. |
| `docker-compose.yaml` | Two services: `postgres` (16-alpine, persistent volume) and `omnigent` (built from the Dockerfile, depends on postgres healthcheck). Build context is `../..` (repo root). |
| `.env.example` | Documents every env var the compose file passes through: `POSTGRES_PASSWORD`, `OMNIGENT_PORT`, all the `OMNIGENT_AUTH_*` and `OMNIGENT_OIDC_*` vars. |
| `README.md` | Customer-facing quickstart + the OIDC walkthrough (GitHub OAuth, Google Workspace, generic OIDC). |

## Iterating on the image

```bash
# Force a clean rebuild after a Dockerfile or source change
docker compose build --no-cache omnigent

# Reset everything (drops the DB + artifact volumes)
docker compose down -v
docker compose up -d --build
```

`POSTGRES_PASSWORD` is only honored on first init of the data volume.
If you change it in `.env`, you need `docker compose down -v` before
`up -d` or the server will fail to authenticate against the existing
cluster.

## Common debugging

| Symptom | Likely cause | First check |
|---|---|---|
| Root URL returns `{"service":"omnigent",…}` instead of the SPA | npm build didn't produce the bundle inside the container | `docker compose exec omnigent ls /build/omnigent/server/static/web-ui/` — empty = the `web-builder` stage didn't run cleanly. Rebuild with `--no-cache`. |
| `ModuleNotFoundError: No module named 'uvicorn'` at startup | venv copy didn't pick up the install | Sanity-check the Dockerfile's `VIRTUAL_ENV=/opt/venv` is set before the `uv pip install` calls. |
| `psycopg.OperationalError: password authentication failed` | `POSTGRES_PASSWORD` changed in `.env` after the data volume was initialized | `docker compose down -v` then `up -d` (wipes the DB). |
| Web UI loads but new chats hang forever | Expected — runners are external. The UI's landing page prints the CLI command to launch a runner. |

## Extending to a new platform

Cloud Run, Fly.io, Render, k8s, HF Spaces — they all consume the
same image. The platform-specific bit is the manifest (`fly.toml`,
`service.yaml`, Helm chart, Spaces config) and any platform-managed
TLS / DB wiring. Put that under `deploy/<platform>/` next to
`docker/`, with its own README + SKILL.

## Related skills + docs

- [`deploy/README.md`](../README.md) — the deploy-options menu.
- `designs/OIDC_AUTH.md` — full native OIDC design.
