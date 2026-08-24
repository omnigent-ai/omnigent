#!/usr/bin/env bash
# Build the wheels and optional web UI assets needed for a Databricks Apps
# deployment of Omnigent.
#
# Inputs:
#   SKIP_WEB_UI=1         Skip the web SPA build for API-only deployments.
#   WEB_UI_OUT_NAME=<name> Move the built SPA to dist/<name> instead of leaving
#                         it in the wheel's package data.
#
# Outputs:
#   dist/omnigent-<version>-py3-none-any.whl
#   dist/omnigent_client-<version>-py3-none-any.whl
#   dist/omnigent_ui_sdk-<version>-py3-none-any.whl
#   dist/$WEB_UI_OUT_NAME/  SPA assets, when WEB_UI_OUT_NAME is set

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
    if [[ -n "${WEB_UI_OUT_NAME:-}" ]]; then
        if [[ ! "${WEB_UI_OUT_NAME}" =~ ^[A-Za-z0-9_-]+$ ]]; then
            echo "ERROR: WEB_UI_OUT_NAME must be a bare directory name matching" \
                 "[A-Za-z0-9_-]+ (got '${WEB_UI_OUT_NAME}')" >&2
            exit 1
        fi
        mkdir -p "${REPO_ROOT}/dist"
        web_ui_out_dir="${REPO_ROOT}/dist/${WEB_UI_OUT_NAME}"
        echo "==> Moving SPA out of the wheel into ${web_ui_out_dir}"
        rm -rf "${web_ui_out_dir}"
        mv omnigent/server/static/web-ui "${web_ui_out_dir}"
        # Prevent setup.py from putting the moved SPA back into the wheel.
        export OMNIGENT_SKIP_WEB_UI=true
    fi
else
    echo "==> SKIP_WEB_UI=1: skipping web build"
fi

echo "==> Building omnigent-client wheel"
uv build --wheel --out-dir dist/ sdks/python-client/

echo "==> Building omnigent-ui-sdk wheel"
uv build --wheel --out-dir dist/ sdks/ui/

echo "==> Building omnigent wheel"
uv build --wheel --out-dir dist/ .

echo ""
echo "Built wheels:"
ls -1 dist/
