# Omnigent images: server (default target) + host (`--target host`).
#
# The default/final target builds the server image (external-runner
# mode): the FastAPI / WebSocket coordinator — HTTP + SSE routes, the
# runner WebSocket tunnel acceptor, and the SQLAlchemy stores. It does
# NOT execute agent harnesses — runners run on the user's local machine
# and dial in via WS /v1/runner/tunnel.
#
# As a consequence: no tmux, no git, no harness SDK runtime
# requirements in the final image. The omnigent package is still
# installed in full (its pyproject pulls claude-agent-sdk +
# openai-agents transitively), so the bits are on disk; they are
# simply never imported because HarnessProcessManager is never
# invoked in this deployment mode.
#
# The `host` target is the inverse: a prebaked Omnigent HOST image for
# remote sandboxes (e.g. `omnigent sandbox create --provider modal`)
# and server-launched managed hosts. It bakes the full omnigent
# install plus the tools a host needs at runtime — git (workspaces /
# worktrees), tmux (terminal sessions spawned by native harnesses),
# and the coding-harness CLIs (claude / codex / pi, via Node) — and
# skips everything server-only: no SPA bundle, no psycopg, no
# uvicorn entrypoint. Modal's `Image.from_registry` requirements shape
# it: built for linux/amd64, `python` + `pip` on $PATH, and CMD-only
# (an ENTRYPOINT that doesn't exec its args would block Modal's own
# runtime from running).
#
# Build (from repo root):
#
#   docker build -t omnigent-server:latest \
#                -f deploy/docker/Dockerfile .
#
#   docker build -t omnigent-host:latest --target host \
#                -f deploy/docker/Dockerfile .
#
# Behind corporate package proxies (the host target also installs the
# harness CLIs from npm, so it needs the npm proxy too):
#
#   docker build -t omnigent-server:latest \
#                -f deploy/docker/Dockerfile \
#                --build-arg PYPI_INDEX_URL=https://pypi-proxy.example.com/simple \
#                --build-arg NPM_CONFIG_REGISTRY=https://npm-proxy.example.com .
#
# Or just use the bundled compose file (Postgres included):
#
#   cd deploy/docker && docker compose up -d

# Must satisfy pyproject requires-python (>=3.12); 3.11 fails dependency resolution.
ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=20

# ── Web UI builder ──────────────────────────────────────
# Builds the ap-web SPA so `docker build` works from a clean checkout —
# no separate `cd ap-web && npm run build` step, no "SPA bundle missing"
# hard-fail. vite.config emits to ../omnigent/server/static/web-ui
# (relative to ap-web/), so from /web/ap-web the bundle lands at
# /web/omnigent/server/static/web-ui, which the server builder overlays.
# Server-only: the host target never reaches this stage.
#
# Behind a corporate npm proxy, pass it through:
#   --build-arg NPM_CONFIG_REGISTRY=https://npm-proxy.example.com
FROM node:${NODE_VERSION}-slim AS web-builder
ARG NPM_CONFIG_REGISTRY=
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
WORKDIR /web/ap-web
# Manifests first so the install layer caches across pure source edits.
COPY ap-web/package.json ap-web/package-lock.json ./
RUN npm install --no-audit --no-fund
COPY ap-web/ ./
RUN npm run build

# ── Python builder (shared: server + host) ──────────────
# Installs the package (and its transitive native-extension deps) into
# a virtualenv the runtime stages copy verbatim. Keeps build-essential
# out of the shipped images. Deliberately SPA-free and psycopg-free so
# the host target can build without node; the server-only additions
# live in server-builder below.
FROM python:${PYTHON_VERSION}-slim AS builder

ARG PYPI_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --index-url ${PYPI_INDEX_URL} --no-cache-dir uv

WORKDIR /build

# Manifests + SDK path-deps first so the install layer caches across
# pure source edits.
COPY pyproject.toml setup.py ./
# License + third-party attribution travel into the published images (the host
# and runtime targets COPY /build below), since the image redistributes the
# dependency bytes baked in by the install step.
COPY LICENSE NOTICE ./
COPY sdks/ ./sdks/
COPY omnigent/ ./omnigent/
# The built-in polly agent's packaged bundle (omnigent/resources/examples/
# polly) is a symlink into the top-level examples/ tree. Ship examples/ so the
# symlink resolves at boot; without it the new-session picker silently drops
# polly (the seeder skips it when its bundle is absent). ~120K of YAML.
COPY examples/ ./examples/

# Standalone venv we can copy out as a single directory. VIRTUAL_ENV
# tells uv to install into this venv (not the slim image's system
# Python) — without it, uv falls back to system Python and the
# copied /opt/venv lands in the runtime stage empty.
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# Install omnigent. Editable install (-e) is required because
# pyproject.toml declares the sibling SDKs as editable path deps via
# [tool.uv.sources]. The runtime stages preserve /build/ so the venv's
# .pth references stay valid.
RUN uv pip install --no-cache-dir --index-url ${PYPI_INDEX_URL} -e .

# ── Server builder ──────────────────────────────────────
# Server-only additions on top of the shared builder: the SPA bundle
# and the Postgres driver.
FROM builder AS server-builder

ARG PYPI_INDEX_URL=https://pypi.org/simple

# Overlay the SPA bundle built in the web-builder stage, so a clean
# checkout (with no prebuilt bundle on the host) still produces a
# complete image. This replaces the old "prebuild or hard-fail" check.
COPY --from=web-builder /web/omnigent/server/static/web-ui ./omnigent/server/static/web-ui
RUN test -f ./omnigent/server/static/web-ui/index.html \
    || (echo "ERROR: SPA bundle missing after web-builder stage — check the ap-web build." && exit 1)

# psycopg[binary] is not a baseline dep — pulled in by the
# [databricks] extra in pyproject — so add it explicitly here.
RUN uv pip install --no-cache-dir --index-url ${PYPI_INDEX_URL} 'psycopg[binary]>=3.1,<4'

# Optional managed-sandbox provider extras for the SERVER (the launcher imports
# the provider SDK — e.g. the kubernetes client for `sandbox.provider:
# kubernetes`). Off by default; the runner host image needs none of these. Build
# with `--build-arg OMNIGENT_EXTRAS=kubernetes` (comma-separate for several).
ARG OMNIGENT_EXTRAS=
RUN if [ -n "${OMNIGENT_EXTRAS}" ]; then \
        uv pip install --no-cache-dir --index-url ${PYPI_INDEX_URL} -e ".[${OMNIGENT_EXTRAS}]"; \
    fi

# ── Node alias stage ─────────────────────────────────────
# Pure alias so the host stage can COPY node out of a version-pinned
# image; contributes no layers of its own.
FROM node:${NODE_VERSION}-slim AS node-runtime

# ── Host runtime (`--target host`) ──────────────────────
# Prebaked Omnigent host for remote sandboxes: full omnigent install,
# git + tmux for harness runtime needs, bubblewrap to OS-sandbox the
# native harness terminals (mandatory and fail-loud on Linux), curl + CA
# certificates for outbound HTTPS and in-sandbox downloads, plus the
# coding-harness CLIs (claude / codex / pi) so claude-sdk, claude-native,
# codex, and pi agents can run in managed sandboxes without an in-sandbox
# install. No SPA, no psycopg, no server entrypoint.
#
# Also carries two additions required only by the NVIDIA OpenShell provider
# (deploy/openshell/README.md) and inert for the root-based providers
# (Modal / Daytona / CoreWeave):
#   - iproute2 / nftables: OpenShell puts each sandbox in its own network
#     namespace and routes egress through a policy proxy; the supervisor
#     shells out to `ip` to build that netns and refuses to start a sandbox
#     without it. The other providers create no namespaces, so it sits unused.
#   - a non-root `sandbox` user/group: OpenShell drops privileges to a user
#     literally named `sandbox` before running the agent (defense in depth)
#     and fails closed if it is absent. The other providers run as root and
#     never reference it.
FROM python:${PYTHON_VERSION}-slim AS host
ARG NPM_CONFIG_REGISTRY=
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}

# IS_SANDBOX: devcontainer-convention flag consulted by Claude Code —
# it refuses --dangerously-skip-permissions under root without it, and
# sandbox containers run as root. The host forwards it to runners (see
# _RUNNER_ENV_ALLOWLIST in omnigent/host/connect.py) so the
# claude-sdk harness can start. Only this image sets it; laptops never
# have it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    IS_SANDBOX=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tmux bubblewrap curl ca-certificates \
      iproute2 nftables \
 && rm -rf /var/lib/apt/lists/*

# OpenShell-required non-root user (see the header note). UID/GID in the high
# range per OpenShell's bring-your-own-container guidance, so that without
# user-namespace remapping the sandbox user maps to an unprivileged, unused
# host id. Unused by the root-based providers.
RUN groupadd -g 1000660000 sandbox \
 && useradd -m -u 1000660000 -g sandbox sandbox

# Git credential helper for private repositories over HTTPS: answers
# `git credential get` from GIT_TOKEN / GIT_USERNAME in the
# environment (injected via Modal secrets — see deploy/modal/README.md
# "Git credentials"), so the managed launch's repository clone AND the
# agent's later fetch/push authenticate without writing credentials to
# disk. Emits nothing when GIT_TOKEN is unset, leaving anonymous
# clones of public repositories untouched. GIT_USERNAME defaults to
# x-access-token (GitHub's token-auth username; GitLab users set
# GIT_USERNAME=oauth2). --system so it applies to any sandbox user.
RUN git config --system credential.helper \
      '!f() { [ "$1" = get ] || return 0; [ -n "$GIT_TOKEN" ] || return 0; printf "username=%s\npassword=%s\n" "${GIT_USERNAME:-x-access-token}" "$GIT_TOKEN"; }; f'

# Node runtime for the harness CLIs, copied from the official image
# (same Debian base as python-slim, so the binary is ABI-compatible)
# rather than apt — keeps the version pinned to NODE_VERSION and skips
# nodesource setup. npm/npx are symlinks into npm's node_modules.
# (`node-runtime` is the alias stage below — COPY --from does not
# expand build args, so the FROM line does the ${NODE_VERSION} part.)
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# The harness CLI set mirrors omnigent/onboarding/harness_install.py
# (the binary/package map `omnigent setup` installs from) — keep the
# two in sync. Unpinned on purpose: the official image is rebuilt by
# CI, so it tracks the same "latest" a laptop install would get.
RUN npm install -g --no-audit --no-fund \
      @anthropic-ai/claude-code \
      @openai/codex \
      @earendil-works/pi-coding-agent \
 && npm cache clean --force

# Preserve /build/ — the venv's editable install .pth files reference
# /build/omnigent and /build/sdks/* by absolute path. Copying these to
# /app/ would break the import paths silently.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build /build

# Sandbox launchers exec commands through `bash -lc`, and Debian's
# /etc/profile unconditionally resets PATH for login shells — the ENV
# PATH above would silently drop off and `omnigent` / `pip` would
# resolve to the system Python instead of the venv. profile.d snippets
# are sourced after that reset, so this puts the venv back in front.
RUN echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/omnigent-venv.sh

WORKDIR /root

# CMD only — Modal's `Image.from_registry` (and the sandbox launchers
# generally) supply their own entrypoint command; an ENTRYPOINT here
# would hijack it. The hold-open default mirrors what `omnigent
# sandbox create` runs so a bare `docker run` behaves the same way.
CMD ["sleep", "infinity"]

# ── Server runtime (default target) ─────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

# curl + ca-certificates only — for the HEALTHCHECK and for outbound
# HTTPS to LLM gateways / external services.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Preserve /build/ in the runtime stage — the venv's editable install
# .pth files reference /build/omnigent and /build/sdks/* by absolute
# path. Copying these to /app/ would break the import paths silently.
COPY --from=server-builder /opt/venv /opt/venv
COPY --from=server-builder /build /build
COPY deploy/docker/entrypoint.py /app/entrypoint.py

WORKDIR /app

# /data is mounted as a persistent volume by docker-compose for the
# artifact store.
RUN mkdir -p /data/artifacts

ENV PORT=8000 \
    HOST=0.0.0.0 \
    ARTIFACT_DIR=/data/artifacts

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["python", "/app/entrypoint.py"]
