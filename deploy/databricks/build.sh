#!/usr/bin/env bash
# Build the wheels and optional web UI assets needed for a Databricks Apps
# deployment of Omnigent.
#
# Inputs:
#   SKIP_WEB_UI=1         Skip the web SPA build for API-only deployments.
#   EXTERNALIZE_WEB_UI=1  Build the SPA, then archive it outside the wheel so
#                         the Databricks Apps source sync uploads one file.
#   OMNIGENT_ENABLE_CANVAS=true
#                         Build the first-party Canvas extension wheel. Canvas
#                         is disabled by default.
#
# Outputs:
#   dist/omnigent-<version>-py3-none-any.whl
#   dist/omnigent_client-<version>-py3-none-any.whl
#   dist/omnigent_ui_sdk-<version>-py3-none-any.whl
#   dist/omnigent_canvas-<version>-py3-none-any.whl (when explicitly enabled)
#   dist/web-ui.tar.gz    SPA archive, when EXTERNALIZE_WEB_UI=1

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
    if [[ "${EXTERNALIZE_WEB_UI:-}" == "1" ]]; then
        # One archive avoids a Workspace round-trip for every one of the
        # SPA's hundreds of hashed chunks. The fixed destination also means
        # no caller-controlled path is ever passed to rm or mv.
        echo "==> Packing SPA into dist/web-ui.tar.gz (excluded from wheel)"
        mkdir -p "${REPO_ROOT}/dist"
        rm -rf "${REPO_ROOT}/dist/web-ui" "${REPO_ROOT}/dist/web-ui.tar.gz"
        tar -czf "${REPO_ROOT}/dist/web-ui.tar.gz" \
            -C "${REPO_ROOT}/omnigent/server/static/web-ui" .
        rm -rf "${REPO_ROOT}/omnigent/server/static/web-ui"
        # Prevent setup.py from putting the archived SPA back into the wheel.
        export OMNIGENT_SKIP_WEB_UI=true
    fi
else
    echo "==> SKIP_WEB_UI=1: skipping web build"
    # The wheel build hook uses its own opt-out for API-only packages.
    export OMNIGENT_SKIP_WEB_UI=true
fi

echo "==> Building omnigent-client wheel"
uv build --wheel --out-dir dist/ sdks/python-client/

echo "==> Building omnigent-ui-sdk wheel"
uv build --wheel --out-dir dist/ sdks/ui/

echo "==> Building omnigent wheel"
uv build --wheel --out-dir dist/ .

CANVAS_ENABLED=0
case "${OMNIGENT_ENABLE_CANVAS:-}" in
    1|true|TRUE|True|yes|YES|Yes) CANVAS_ENABLED=1 ;;
esac

if [[ "${SKIP_WEB_UI:-}" != "1" && "${CANVAS_ENABLED}" == "1" ]]; then
    echo "==> Building Canvas extension wheel"
    uv build --wheel --out-dir dist/ extensions/canvas/
fi

echo ""
echo "Built wheels:"
ls -1 dist/
