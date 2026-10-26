# Image for Anthropic's OSS Scanner: the repo root is the build context, the checkout lives at /src, and the
# audit runs offline. Python deps match CI's unit-test lane; the web UI builds as in deploy/docker/Dockerfile.
ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

# COPY --from does not expand build args, so this alias stage pins the Node version.
FROM node:${NODE_VERSION}-slim AS node-runtime

FROM python:${PYTHON_VERSION}-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# OMNIGENT_SKIP_WEB_UI: setup.py would otherwise build the web UI inside `uv sync`; it is built in its own
# step below. Electron and Playwright download their binaries on install, and nothing here launches either.
ENV DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    OMNIGENT_SKIP_WEB_UI=true \
    ELECTRON_SKIP_BINARY_DOWNLOAD=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# The packages CI installs for the tests: ripgrep (the Grep tool), bubblewrap (the linux_bwrap sandbox), tmux
# (harness terminals) and dbus-daemon + gnome-keyring (keyring tests). git backs workspaces and worktrees.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git ripgrep bubblewrap tmux dbus-daemon gnome-keyring \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Scanner contract: the checkout lives inside the image, at /src.
COPY . /src
WORKDIR /src

# The pnpm version package.json pins (without any Corepack hash suffix) and the uv version
# .github/actions/setup-uv pins.
RUN npm install -g "$(node -p 'require("./package.json").packageManager.split("+")[0]')" \
    && UV_VERSION="$(awk '/^  version:/{f=1} f&&/default:/{gsub(/"/,"",$2); print $2; exit}' .github/actions/setup-uv/action.yml)" \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

# The web UI, emitted into omnigent/server/static/web-ui so a local server serves it. --filter web leaves
# out the Electron app and the other workspace packages.
RUN pnpm install --frozen-lockfile --filter web \
    && pnpm --filter web run build

# Every package the CI unit-test lane installs, from the committed lock file, into /src/.venv.
RUN uv sync --locked --extra all --extra openshell --extra kubernetes --group test

# The scanner runs offline, so `uv run` must use the prebuilt venv without resolving or syncing.
# OMNIGENT_TEST_TIMEOUT_SCALE stretches the tests' hang guards, as CI does on its shared runners.
ENV UV_FROZEN=1 \
    UV_NO_SYNC=1 \
    UV_OFFLINE=1 \
    VIRTUAL_ENV=/src/.venv \
    PATH=/src/.venv/bin:$PATH \
    OMNIGENT_TEST_TIMEOUT_SCALE=4

# A best-effort smoke check of the auth, permission and policy tests; failures don't abort the build.
# Other tests: `pytest <path> -m "not databricks"`.
RUN pytest tests/server/test_auth_mode.py tests/server/test_permissions.py \
        tests/server/test_public_sharing_policy.py tests/policies \
        -m "not databricks" -q -p no:cacheprovider \
    || echo "WARNING: pytest reported failures"
