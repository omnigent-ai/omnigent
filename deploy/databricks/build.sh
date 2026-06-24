#!/usr/bin/env bash
# Build the wheels and optional web UI assets needed for a Databricks Apps
# deployment of Omnigent.
#
# Inputs:
#   SKIP_WEB_UI=1   Skip the ap-web SPA build for API-only deployments.
#
# Outputs:
#   dist/omnigent-<version>-py3-none-any.whl
#   dist/omnigent_client-<version>-py3-none-any.whl
#   dist/omnigent_ui_sdk-<version>-py3-none-any.whl

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
    echo "==> Building ap-web SPA into omnigent/server/static/web-ui/"
    cd ap-web
    npm install
    npm run build
    cd "${REPO_ROOT}"
else
    echo "==> SKIP_WEB_UI=1: skipping ap-web build"
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
