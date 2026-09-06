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
# and the coding-harness CLIs (claude / codex / pi, via Node; kiro-cli via
# Kiro's installer) — and
# skips everything server-only: no SPA bundle, no psycopg, no
# uvicorn entrypoint. Modal's `Image.from_registry` requirements shape
# it: `python` + `pip` on $PATH, and CMD-only (an ENTRYPOINT that
# doesn't exec its args would block Modal's own runtime from running).
#
# Both images publish multi-arch (linux/amd64 + linux/arm64) — see
# .github/workflows/oss-publish-images.yml — so they run natively on
# arm64 hosts (Apple Silicon laptops, arm64 clusters) as well as amd64.
# Nothing below is arch-specific: the python/node base images are
# multi-arch and apt/pip/npm/COPY-from-node all resolve per-arch under
# buildx. Amd64-only consumers (Modal, Daytona, CoreWeave) keep pulling
# the amd64 variant from the manifest list, unchanged.
#
# Build (from repo root):
#
#   docker build -t omnigent-server:latest \
#                -f deploy/docker/Dockerfile .
#
#   docker build -t omnigent-host:latest --target host \
#                -f deploy/docker/Dockerfile .
#
# To bake extra harness CLIs into a host image beyond the default set, build
# with EXTRA_HARNESS_CLIS="goose jcode opencode" — harness names, optional
# @version pins, or npm:<pkg-spec> entries (see deploy/docker/README.md and
# deploy/docker/install-harness-cli.sh for the supported names).
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
ARG NODE_VERSION=22

# ── Web UI builder ──────────────────────────────────────
# Builds the web SPA so `docker build` works from a clean checkout —
# no separate `cd web && npm run build` step, no "SPA bundle missing"
# hard-fail. vite.config emits to ../omnigent/server/static/web-ui
# (relative to web/), so from /web/web the bundle lands at
# /web/omnigent/server/static/web-ui, which the server builder overlays.
# Server-only: the host target never reaches this stage.
#
# Behind a corporate npm proxy, pass it through:
#   --build-arg NPM_CONFIG_REGISTRY=https://npm-proxy.example.com
FROM node:${NODE_VERSION}-slim AS web-builder
ARG NPM_CONFIG_REGISTRY=
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
WORKDIR /web

# Workspace manifests + lockfile first so the install layer caches across pure
# source edits. The electron package JSON is included so the root workspace is
# structurally complete, but we --filter web to avoid downloading Electron.
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY web/package.json ./web/
COPY web/electron/package.json ./web/electron/

RUN npm install -g pnpm@11.15.1
RUN pnpm install --frozen-lockfile --filter web

COPY web/ ./web/
RUN pnpm --filter web run build

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
    || (echo "ERROR: SPA bundle missing after web-builder stage — check the web build." && exit 1)

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
# git + tmux for harness runtime needs, procps + lsof so the antigravity-native
# executor can discover agy's connect-RPC port (antigravity_native_rpc.py uses
# `pgrep` in _list_agy_pids to find agy processes — with a /proc-scan fallback if
# pgrep is absent — then `lsof` in resolve_language_server_port to read their
# loopback ports; lsof has NO fallback, so a missing lsof silently yields "no
# port" and breaks web-turn injection), bubblewrap to OS-sandbox the
# native harness terminals (mandatory and fail-loud on Linux), curl + CA
# certificates for outbound HTTPS and in-sandbox downloads, plus the
# coding-harness CLIs (claude / codex / pi / kiro-cli) so claude-sdk,
# claude-native, codex, pi, and kiro-native agents can run in managed
# sandboxes without an in-sandbox install. No SPA, no psycopg, no server
# entrypoint.
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

# bzip2 ships on every host so the goose EXTRA_HARNESS_CLIS row works without
# another package-list edit (goose's release archive is .tar.bz2).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git tmux procps lsof bubblewrap curl ca-certificates unzip bzip2 \
      iproute2 nftables \
 && rm -rf /var/lib/apt/lists/*

# OpenShell-required non-root user (see the header note). UID/GID in the high
# range per OpenShell's bring-your-own-container guidance, so that without
# user-namespace remapping the sandbox user maps to an unprivileged, unused
# host id. Unused by the root-based providers.
RUN groupadd -g 1000660000 sandbox \
 && useradd -m -d /sandbox -u 1000660000 -g sandbox sandbox

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

# Kiro CLI is not published as an npm package, and its installer has NO version
# flag — `curl …/install | bash` always fetches `latest`, so the image was
# non-deterministic. The kiro-native harness is behaviorally coupled to a
# specific kiro-cli build (Escape-interrupt leaves an empty composer, the
# bracketed-paste multi-line path, the session-JSONL layout — all verified
# against 2.10.0; grep `kiro-cli 2.10.0`). So pin it the same way as `agy` below:
# fetch the immutable per-arch zip from the versioned CDN path and verify its
# sha256, run the package's own (network-free) install.sh, then copy the binaries
# onto a system PATH dir every sandbox user shares. A trailing `kiro-cli
# --version` check asserts the unpacked binary really is the pinned version — a
# cheap sanity guard atop the sha256. To adopt a new kiro-cli: re-verify the
# coupled behavior, then bump KIRO_CLI_VERSION + both
# SHA256s (the `sha256` fields in
# https://prod.download.cli.kiro.dev/stable/latest/manifest.json). Keep in sync
# with deploy/docker/Dockerfile.ubi.
ARG KIRO_CLI_VERSION=2.10.0
ARG KIRO_CLI_SHA256_AMD64=be9d8b6d7c44f93a83ca22466043d98ad058e6ed3c12fffd068f3fb8a60b3b70
ARG KIRO_CLI_SHA256_ARM64=0afb37399b9e2847c2f2e3f5d9052c8bc52bbf1e30401ea284a602661bce34bc
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64)  asset="kirocli-x86_64-linux.zip";  sha="$KIRO_CLI_SHA256_AMD64" ;; \
      aarch64) asset="kirocli-aarch64-linux.zip"; sha="$KIRO_CLI_SHA256_ARM64" ;; \
      *) echo "ERROR: unsupported arch '$(uname -m)' for kiro-cli" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/kiro.zip "https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/${asset}"; \
    echo "${sha}  /tmp/kiro.zip" | sha256sum -c -; \
    unzip -q /tmp/kiro.zip -d /tmp/kiro; \
    KIRO_CLI_SKIP_SETUP=1 sh /tmp/kiro/kirocli/install.sh; \
    install -m 0755 /root/.local/bin/kiro-cli /usr/local/bin/kiro-cli; \
    if [ -f /root/.local/bin/kiro-cli-chat ]; then \
      install -m 0755 /root/.local/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat; \
    fi; \
    rm -rf /tmp/kiro /tmp/kiro.zip; \
    installed="$(/usr/local/bin/kiro-cli --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"; \
    [ "$installed" = "$KIRO_CLI_VERSION" ] || { \
      echo "ERROR: kiro-cli reports '${installed:-<none>}', expected '$KIRO_CLI_VERSION'." >&2; exit 1; }; \
    echo "kiro-cli ${KIRO_CLI_VERSION} pinned (sha256 verified)"
# Antigravity CLI (`agy`) — the antigravity-native harness shells out to `agy`
# on the host, launching it in a tmux pane (see omnigent/antigravity_native*.py),
# so a managed host image must carry it. It is NOT an npm package
# (harness_install.py lists agy as a non-npm, installer-script harness), so it
# can't join the `npm install -g` set above. The tarball holds a single
# self-contained ``antigravity`` binary; install it as ``agy`` on a system PATH
# dir every user shares (its bootstrapper default ~/.local/bin is per-user and
# off the venv PATH). ``test -x`` fails the build loudly if the layout changes.
#
# Version + integrity pin: the native harness is behaviorally coupled to a
# specific agy build (out-of-order transcript writes, connect-RPC quirks, and TUI
# injection are all verified against 1.1.16 — grep ``agy 1.1.16`` under
# omnigent/antigravity_native*). The official ``install.sh`` bootstrapper has NO
# version flag — it always fetches the LATEST build from an auto-updater manifest
# and old builds are not retained at any stable, reconstructable URL — so it
# cannot pin anything. Instead we fetch the exact, immutable per-arch release
# asset from GitHub and verify its SHA256: this both holds the verified version
# AND fails the build if the bytes ever change underneath us, which is the actual
# supply-chain control (a version-string check alone is not). To adopt a new agy:
# re-verify the coupled behavior, then bump AGY_VERSION and both SHA256s (from
# https://github.com/google-antigravity/antigravity-cli/releases).
ARG AGY_VERSION=1.1.16
ARG AGY_SHA256_AMD64=7742953b7835b457e9102f1357a493913657dfd147435584f609d58356ec085a
ARG AGY_SHA256_ARM64=c71599bb548cf72dfe7a8f6c411b3c7a3623cd4b0f6276bae89c69f769d6c92d
# Exported for install-harness-cli.sh's agy row, so a build that adds
# EXTRA_HARNESS_CLIS=agy pins the same version + sha256 as the install above
# (and UBI — which has no baked agy — pins the same values).
ENV AGY_VERSION=${AGY_VERSION} \
    AGY_SHA256_AMD64=${AGY_SHA256_AMD64} \
    AGY_SHA256_ARM64=${AGY_SHA256_ARM64}
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset="agy_cli_linux_x64.tar.gz"; sha="$AGY_SHA256_AMD64" ;; \
      arm64) asset="agy_cli_linux_arm64.tar.gz"; sha="$AGY_SHA256_ARM64" ;; \
      *) echo "ERROR: unsupported arch '$arch' for agy" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/${asset}"; \
    curl -fsSL -o /tmp/agy.tar.gz "$url"; \
    echo "${sha}  /tmp/agy.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/agy.tar.gz -C /tmp antigravity; \
    install -m 0755 /tmp/antigravity /usr/local/bin/agy; \
    rm -f /tmp/agy.tar.gz /tmp/antigravity; \
    test -x /usr/local/bin/agy; \
    installed="$(/usr/local/bin/agy --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"; \
    if [ "$installed" != "$AGY_VERSION" ]; then \
      echo "ERROR: agy reports '${installed:-<unparseable>}', expected '$AGY_VERSION'." >&2; \
      exit 1; \
    fi; \
    echo "agy ${AGY_VERSION} pinned (sha256 verified)"

# GitHub CLI (`gh`) — the read-only "GitHub" side panel shells out to `gh` in the
# runner (omnigent/runner/github_resource.py: `gh pr view` / `gh pr diff` /
# `gh api .../pulls/<n>/files`) to render the session branch's PR + file diffs, so
# a managed host image must carry it or the panel degrades to a "gh not installed"
# state. Pinned + SHA256-verified per arch, like agy above; to bump, update
# GH_VERSION and both SHA256s from the release's `gh_<ver>_checksums.txt` at
# https://github.com/cli/cli/releases.
ARG GH_VERSION=2.100.0
ARG GH_SHA256_AMD64=e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be
ARG GH_SHA256_ARM64=ea4e7a581a32ccad6cc7923cb1576ac5859ba4b9a16ab22eb8f8a96e78e2e961
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sha="$GH_SHA256_AMD64" ;; \
      arm64) sha="$GH_SHA256_ARM64" ;; \
      *) echo "ERROR: unsupported arch '$arch' for gh" >&2; exit 1 ;; \
    esac; \
    asset="gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${asset}"; \
    echo "${sha}  /tmp/gh.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gh.tar.gz -C /tmp "gh_${GH_VERSION}_linux_${arch}/bin/gh"; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    test -x /usr/local/bin/gh; \
    installed="$(/usr/local/bin/gh --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"; \
    if [ "$installed" != "$GH_VERSION" ]; then \
      echo "ERROR: gh reports '${installed:-<unparseable>}', expected '$GH_VERSION'." >&2; \
      exit 1; \
    fi; \
    echo "gh ${GH_VERSION} pinned (sha256 verified)"

# Copy the venv and source tree. The editable install's .pth files reference
# /build/omnigent and /build/sdks/* -- both denied by the k8s Landlock LSM
# policy. Re-install without -e so the package bytes land in the venv's
# site-packages and imports no longer require /build at runtime.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build /build
RUN pip install --no-cache-dir /build /build/sdks/python-client /build/sdks/ui \
 && ! grep -R --include='*.pth' --include='*.egg-link' -nE '/build(/|$)' /opt/venv/lib/python*/site-packages

# Sandbox launchers exec commands through `bash -lc`, and Debian's
# /etc/profile unconditionally resets PATH for login shells — the ENV
# PATH above would silently drop off and `omnigent` / `pip` would
# resolve to the system Python instead of the venv. profile.d snippets
# are sourced after that reset, so this puts the venv back in front.
RUN echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/omnigent-venv.sh

# Optional extra harness CLIs, off by default: space-separated harness names
# with optional @version pins, or npm:<pkg-spec> — see install-harness-cli.sh
# and deploy/docker/README.md. Kept at the end of the stage so script churn
# doesn't bust the layers above. `set -f` stops glob expansion in the unquoted
# arg list (npm specs may contain *). The script is removed after use.
ARG EXTRA_HARNESS_CLIS=
COPY deploy/docker/install-harness-cli.sh /tmp/install-harness-cli.sh
RUN if [ -n "${EXTRA_HARNESS_CLIS}" ]; then \
        set -f; \
        bash /tmp/install-harness-cli.sh ${EXTRA_HARNESS_CLIS}; \
    fi; \
    rm -f /tmp/install-harness-cli.sh

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
