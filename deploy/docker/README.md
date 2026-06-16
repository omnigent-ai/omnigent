# Omnigent — docker-compose stack

Run the server as a self-contained Docker stack on any host: your
laptop, a VPS, an EC2 instance, a home server, anywhere `docker
compose` runs.

The stack:
- `postgres` — persistent DB on a Docker volume
- `omnigent` — the server image (built from `../Dockerfile`)

Auth is in-process — the server has both header-proxy and native
OIDC modes built in (see [Multi-user mode](#multi-user-mode-oidc)
below). There is no separate auth-proxy container.

## Quickstart (single-user)

```bash
cd deploy/docker
./bootstrap.sh                          # mints POSTGRES_PASSWORD + cookie secret into .env
docker compose up -d
docker compose logs -f omnigent       # ctrl-c when boot is clean
```

`bootstrap.sh` is idempotent — re-running it leaves already-set secrets
alone. If you prefer to manage `.env` yourself, just `cp .env.example
.env` and edit `POSTGRES_PASSWORD` (and `OMNIGENT_OIDC_COOKIE_SECRET`
if you're enabling OIDC) by hand.

Server is on http://localhost:8000. The web UI prints the CLI command
to launch a local runner against it. From your laptop:

```bash
omnigent run path/to/agent.yaml --server http://localhost:8000
```

Reset everything (drops the DB and the artifact store):

```bash
docker compose down -v
```

## Multi-user mode (accounts — default)

Built-in accounts auth: no IdP to register, no proxy to host.
This is the default — `docker compose up -d` brings it up with no
extra env wiring. First boot creates an admin user (named after the
operator's OS user, falling back to `admin` in headless containers)
with a random password that lands in the container logs and on the
persistent volume at `/data/admin-credentials`.

For any deploy reachable through a public domain, also set the
external URL so invite links resolve correctly:

```bash
# Add to .env (bootstrap.sh already minted the cookie secret for you):
OMNIGENT_ACCOUNTS_BASE_URL=https://omnigent.example.com

docker compose up -d
docker compose logs omnigent | grep -A4 "Created initial admin"
```

Copy the random `password` from the log line into the web UI's
login form, then:

- Click your username in the top-right → **Members** → **Invite member**.
- Share the single-use URL with the teammate; they pick their own
  username and password when they redeem it.
- Sign-out lives in the same account menu.

Headless deploy (CI, Cloud Run, etc.) where you can't read the
logs? Pre-seed the password:

```bash
OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD=<your-strong-password>
```

The persistent password file is at `/data/admin-credentials` on
the `artifact-data` volume — survives `docker compose restart`,
deleted by `docker compose down -v`.

## Multi-user mode (OIDC)

Single-user mode trusts everyone who reaches the port and uses the
identity `"local"` for all requests. For a shared deploy, the server
has native OIDC support — it handles the full
login flow itself (`/auth/login`, `/auth/callback`, `/auth/logout`)
with a signed session cookie. No extra container, no Caddy basic-auth
shim, no oauth2-proxy.

### Walkthrough: GitHub OAuth (easiest to register)

1. **Register the OAuth app.** Go to
   https://github.com/settings/developers → New OAuth App. Set the
   callback to `https://<your-host>/auth/callback` (HTTPS is
   strongly recommended; GitHub permits HTTP for testing but warns).

2. **Mint a cookie secret.** `./bootstrap.sh` already did this on the
   quickstart path — `OMNIGENT_OIDC_COOKIE_SECRET` is set in your
   `.env`. If you skipped it, run `openssl rand -hex 32` and paste the
   value yourself.

3. **Edit `.env`:**
   ```bash
   OMNIGENT_AUTH_PROVIDER=oidc
   OMNIGENT_OIDC_ISSUER=https://github.com
   OMNIGENT_OIDC_CLIENT_ID=Iv1.abc123…
   OMNIGENT_OIDC_CLIENT_SECRET=…
   OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.example.com/auth/callback
   # OMNIGENT_OIDC_COOKIE_SECRET is already set by bootstrap.sh — leave it alone.
   ```

4. **Bring it up.**
   ```bash
   docker compose up -d
   ```

   The server will fail loud at startup if any required OIDC env var
   is missing — check `docker compose logs omnigent` if it doesn't
   come up.

5. **Visit the URL** → you should be redirected to GitHub to log in,
   then back to the web UI with a `__Host-ap_session` cookie set.

### Walkthrough: Google Workspace (with domain allowlist)

```bash
OMNIGENT_AUTH_PROVIDER=oidc
OMNIGENT_OIDC_ISSUER=https://accounts.google.com
OMNIGENT_OIDC_CLIENT_ID=…apps.googleusercontent.com
OMNIGENT_OIDC_CLIENT_SECRET=…
OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.example.com/auth/callback
OMNIGENT_OIDC_COOKIE_SECRET=<64-hex-chars>
OMNIGENT_OIDC_ALLOWED_DOMAINS=example.com,subsidiary.example.com
```

`ALLOWED_DOMAINS` is critical when the OAuth consent screen is
"External" — without it, any Google account on the planet can log in.

### Generic OIDC (Okta, Auth0, Keycloak, Entra ID)

Any IdP that publishes `/.well-known/openid-configuration` works.
Set `OMNIGENT_OIDC_ISSUER` to the base URL; the server fetches
discovery at startup.

```bash
OMNIGENT_AUTH_PROVIDER=oidc
OMNIGENT_OIDC_ISSUER=https://your-tenant.okta.com
OMNIGENT_OIDC_CLIENT_ID=…
OMNIGENT_OIDC_CLIENT_SECRET=…
OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.example.com/auth/callback
OMNIGENT_OIDC_COOKIE_SECRET=<64-hex-chars>
```

### HTTPS for the callback URL

Most IdPs require HTTPS for non-localhost redirect URIs, and the
session cookie uses the `__Host-` prefix which browsers only
accept over HTTPS. Three options:

1. **Use the bundled Caddy overlay** (easiest — any VPS / EC2 / home
   server with a public domain):

   ```bash
   # In .env:
   OMNIGENT_DOMAIN=omnigent.example.com
   OMNIGENT_ACME_EMAIL=you@example.com      # optional, for Let's Encrypt notices

   # Point DNS A/AAAA records at the host, then:
   docker compose -f docker-compose.yaml -f docker-compose.https.yaml up -d
   ```

   Caddy auto-provisions and renews a Let's Encrypt cert; the
   omnigent container stops being directly exposed and only :80 +
   :443 are published. Requires Docker Compose 2.24+ for the overlay's
   `!reset` directive. See `Caddyfile` for the (3-line) config.

2. **Behind an existing reverse proxy** — point your proxy at
   `omnigent:8000` over the docker network (or `127.0.0.1:8000`
   from the host). Examples: AWS ALB with ACM cert, Cloudflare in
   "Full" SSL mode, Fly.io / Cloud Run / Render platform certs.

## Header-proxy mode (for deploys behind an existing SSO proxy)

If you already have oauth2-proxy, Databricks Apps, AWS ALB OIDC,
Tailscale Funnel, or any other proxy that injects
`X-Forwarded-Email`, set `OMNIGENT_AUTH_PROVIDER=header`. The
server will reject requests without the header.

```bash
OMNIGENT_AUTH_PROVIDER=header
```

**Security note:** in this mode the proxy is responsible for
stripping any inbound `X-Forwarded-Email` from the client request —
otherwise any visitor can spoof an identity. The server trusts
whatever value reaches it.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_PASSWORD` | *required* | DB password for the bundled Postgres container. |
| `POSTGRES_USER` / `POSTGRES_DB` | `omnigent` | DB user + database name. |
| `OMNIGENT_PORT` | `8000` | Host port the server is published on. |
| `OMNIGENT_AUTH_ENABLED` | `1` (in compose) | Master auth switch. `1` → accounts (or oidc if `OMNIGENT_OIDC_ISSUER` is set); `0` → single-user local mode (every request is the shared `local` user — local dev only, never shared deploys). |
| `OMNIGENT_AUTH_PROVIDER` | unset | Escape hatch to pin a mode explicitly: `header` / `accounts` / `oidc`. Overrides the `AUTH_ENABLED` auto-selection. |
| `OMNIGENT_OIDC_*` | unset | OIDC config — required in oidc mode (issuer set, or `AUTH_PROVIDER=oidc`). See `.env.example`. |
| `PYPI_INDEX_URL` | `https://pypi.org/simple` | Build-time PyPI index — override only behind a corporate proxy. |

`DATABASE_URL` and `ARTIFACT_DIR` are computed by compose and
injected into the container.

## Managed Docker sandboxes (self-contained)

The managed-Docker overlay lets the **server provision one sibling
`omnigent-host` container per managed session** — no external SaaS sandbox,
no bring-your-own host. Layer `docker-compose.managed.yaml` on the base stack:

```bash
cd deploy/docker
./bootstrap.sh                                          # POSTGRES_PASSWORD etc.
printf "DOCKER_GID=%s\n" "$(getent group docker | cut -d: -f3)" >> .env
cp config.managed.example.yaml config.managed.yaml      # edit sandbox.docker.env if needed
docker pull ghcr.io/omnigent-ai/omnigent-host:latest    # the sandbox image
docker compose -f docker-compose.yaml -f docker-compose.managed.yaml up -d
```

Then create a session with `host_type: "managed"` (the provider comes from
the server's `sandbox.provider: docker` config, **not** a request field). The
server provisions a container, runs `omnigent host` inside it, and binds the
session to it; on session delete the container is removed. Orphans left by a
server crash are reaped on the next startup and on a periodic sweep.

### Build the host image locally (no registry needed)

You do **not** need access to the published `ghcr.io/omnigent-ai/omnigent-host`
image — build it from this repo's Dockerfile `host` target and point the
provider at your local tag:

```bash
# from the repo root
docker build -t omnigent-host:local --target host -f deploy/docker/Dockerfile .
# then either set it in .env …
echo "OMNIGENT_DOCKER_HOST_IMAGE=omnigent-host:local" >> deploy/docker/.env
# … or in config.managed.yaml under sandbox.docker.image
```

The `host` target bakes the full omnigent install plus git, tmux, bubblewrap,
and the harness CLIs (claude / codex / pi). For a multi-node setup, push the
tag to your own registry and reference that instead.

### `DOCKER_GID` (required)

The server runs as a non-root user (UID 10001), so a bare
`/var/run/docker.sock` bind-mount (typically `root:docker`, mode `0660`) is
**not** readable. The overlay adds the server to the host's docker group via
`group_add: ["${DOCKER_GID}"]`. Set `DOCKER_GID` in `.env` to
`getent group docker | cut -d: -f3`. If it's wrong, the first managed launch
fails fast at `prepare()`'s `client.ping()`.

### Auth: single-user

The overlay sets `OMNIGENT_AUTH_ENABLED=0` and leaves `OMNIGENT_AUTH_PROVIDER`
empty. Managed hosts open a per-session **runner tunnel** that authenticates
with a resolved user credential the launch token lacks, so under multi-user
`accounts` auth that tunnel is refused. Run the managed profile single-user
(trusted tenant), or front the server with a header/OIDC proxy for multi-user.
Do **not** set `OMNIGENT_AUTH_PROVIDER` under `AUTH_ENABLED=0` — an explicit
provider overrides the switch and silently re-enables auth.

### Security posture

- **Docker socket = host root.** Mounting `/var/run/docker.sock` gives the
  server host-root-equivalent control of Docker. Accepted for internal
  deployments; a docker-socket-proxy is the hardening follow-up. The socket is
  mounted into the **server only**, never into sandbox containers.
- **Network segmentation.** Two networks: `omnigent-app` (server + Postgres)
  and `omnigent-sbx` (server + sandbox containers). Sandboxes reach the server
  at `http://omnigent:8000` and get outbound NAT, but have **no route to
  Postgres**. Egress is coarse (Docker NAT) in this MVP; fine-grained egress
  (internal network + egress proxy) is a follow-up.
- **Inner sandbox (bwrap) requires a userns-enabled host.** The agent's
  in-process `linux_bwrap` sandbox and the native harnesses
  (claude-native/codex-native/pi, which always wrap terminals in bwrap on
  Linux) need the host kernel to allow **unprivileged user namespaces**
  (`sysctl kernel.unprivileged_userns_clone=1` on Debian/Ubuntu, or a non-zero
  `user.max_user_namespaces`). On a host where that is disabled, bwrap fails to
  create namespaces inside the unprivileged container — run native harnesses
  only on a userns-enabled host, and run non-native agents with an explicit
  `os_env.sandbox.type: none` (the container is then the isolation boundary).
  The opt-in spike
  `tests/onboarding/sandboxes/test_docker_bwrap_spike.py` verifies both paths
  against the real host image; run it on your target host.
- **Container limits.** `sandbox.docker.resources` (`mem_limit` / `nano_cpus`
  / `pids_limit`) and `sandbox.docker.security` (`security_opt` / `cap_drop`)
  are passed to `docker run`. The shipped example sets
  `security_opt: ["no-new-privileges:true"]`; add `cap_drop: ["ALL"]` only
  after the spike confirms bwrap still activates under it on your host.

## Host image (`--target host`)

The same Dockerfile publishes a second image: the official Omnigent
**host** image, which remote sandboxes boot from so they start in
seconds instead of paying an in-sandbox dependency install. It bakes
the full omnigent install (all three packages + deps, `python` and
`pip` on PATH), `git` (workspaces / worktrees), `tmux` (terminal
sessions spawned by native harnesses), and the coding-harness CLIs —
`claude`, `codex`, and `pi`, with the Node runtime they need — so
claude-sdk / claude-native / codex / pi agents run in sandboxes
without an in-sandbox install. None of the server-only bits are
included (no SPA bundle, no psycopg, no uvicorn entrypoint).

CI publishes it next to the server image, with the same tag scheme:

- `ghcr.io/omnigent-ai/omnigent-host:latest` — tracks main HEAD
  (the default for `omnigent sandbox create --provider modal`)
- `ghcr.io/omnigent-ai/omnigent-host:sha-<short>` — immutable
  per-commit pin
- `ghcr.io/omnigent-ai/omnigent-host:vX.Y.Z` — release tags

Build it locally from the repo root:

```bash
docker build -t omnigent-host:latest --target host \
             -f deploy/docker/Dockerfile .
```

### Using it with the Modal sandbox provider

`omnigent sandbox create --provider modal` boots sandboxes from
`ghcr.io/omnigent-ai/omnigent-host:latest` by default. Your local
checkout's wheels are still built and overlaid on top at create time
(`pip install --force-reinstall --no-deps`), so the sandbox runs
exactly your code — the baked image just supplies the dependency
tree. A checkout that adds a brand-new dependency needs that package
installed manually in the sandbox until the official image rebuilds
with it.

Two environment variables tune the pull:

| Variable | Purpose |
|---|---|
| `OMNIGENT_MODAL_HOST_IMAGE` | Override the image ref, e.g. an org-internal copy (`ghcr.io/<your-org>/omnigent-host:latest`) or a `:sha-<short>` pin. |
| `OMNIGENT_MODAL_REGISTRY_SECRET` | Name of a [Modal secret](https://modal.com/secrets) holding registry credentials for private pulls. Create it with keys `REGISTRY_USERNAME` (your registry username) and `REGISTRY_PASSWORD` (for GHCR: a personal access token with `read:packages`). Unset = anonymous pull. |

### Using it with the Daytona sandbox provider

The same host image backs Daytona-managed sessions (server config
`sandbox.provider: daytona`; Daytona is managed-only — there is no
`omnigent sandbox create --provider daytona` CLI flow). Daytona ingests
the registry image into an internal snapshot on first use (the first
launch from a given image takes minutes; later launches reuse the
snapshot and take seconds). Override the ref with
`OMNIGENT_DAYTONA_HOST_IMAGE` or the server config's
`sandbox.daytona.image`. See
[`deploy/daytona/README.md`](../daytona/README.md) for the
full provider guide (credentials, the free-tier egress relay, and
security considerations).

## Related design docs

- `designs/OIDC_AUTH.md` — full native OIDC design
- `designs/SESSIONS_AUTH.md` — `AuthProvider` contract + permission system
