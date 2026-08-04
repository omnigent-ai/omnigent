#!/usr/bin/env bash
# Build the wheels and optional web UI assets needed for a Databricks Apps
# deployment of Omnigent.
#
# Inputs:
#   SKIP_WEB_UI=1        Skip the web SPA build for API-only deployments.
#   WEB_UI_OUT_DIR=<dir> Move the built SPA here instead of leaving it in the
#                        wheel's package data (see below).
#
# Outputs:
#   dist/omnigent-<version>-py3-none-any.whl
#   dist/omnigent_client-<version>-py3-none-any.whl
#   dist/omnigent_ui_sdk-<version>-py3-none-any.whl
#   $WEB_UI_OUT_DIR/     SPA assets, when WEB_UI_OUT_DIR is set

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This file lives at deploy/databricks/ — two levels deep — so the repo root is
# two parents up.
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${REPO_ROOT}"

# Each Vite build emits uniquely-hashed JS chunk filenames. Without a
# sweep, orphaned chunks from prior builds accumulate in the static
# dir, end up in the main wheel, and push it over the 10 MB Workspace
# upload cap. Always start from a clean slate.
echo "==> Cleaning stale static assets and build outputs"
rm -rf omnigent/server/static/web-ui dist build omnigent.egg-info

if [[ "${SKIP_WEB_UI:-}" != "1" ]]; then
    echo "==> Building web SPA into omnigent/server/static/web-ui/"
    pnpm install --frozen-lockfile --filter web
    pnpm --filter web run build
    if [[ -n "${WEB_UI_OUT_DIR:-}" ]]; then
        # Ship the SPA outside the wheel. Databricks Apps uploads each source
        # file to the Workspace, which rejects files over 10 MB, and ~25 MB of
        # assets push the main wheel past that cap. As loose files every asset
        # is well under it; the server picks them up via OMNIGENT_WEB_UI_DIST.
        echo "==> Moving SPA out of the wheel into ${WEB_UI_OUT_DIR}"
        rm -rf "${WEB_UI_OUT_DIR}"
        mkdir -p "$(dirname "${WEB_UI_OUT_DIR}")"
        mv omnigent/server/static/web-ui "${WEB_UI_OUT_DIR}"
    fi
else
    echo "==> SKIP_WEB_UI=1: skipping web build"
fi

# setup.py's build_py hook builds the SPA into the package whenever the bundle
# is missing. That would undo the move above, and it quietly re-adds the SPA to
# a SKIP_WEB_UI=1 wheel. This script owns the SPA build, so opt the backend out.
export OMNIGENT_SKIP_WEB_UI=true

echo "==> Building omnigent-client wheel"
uv build --wheel --out-dir dist/ sdks/python-client/

echo "==> Building omnigent-ui-sdk wheel"
uv build --wheel --out-dir dist/ sdks/ui/

echo "==> Building omnigent wheel"
uv build --wheel --out-dir dist/ .

echo ""
echo "Built wheels:"
ls -1 dist/
