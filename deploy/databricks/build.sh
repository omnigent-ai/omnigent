#!/usr/bin/env bash
# Build the wheels and optional web UI assets needed for a Databricks Apps
# deployment of Omnigent.
#
# Inputs:
#   SKIP_WEB_UI=1         Skip the web SPA build for API-only deployments.
#   WEB_UI_OUT_NAME=<name> Move the built SPA to dist/<name> instead of leaving
#                         it in the wheel's package data (see below). A bare
#                         directory NAME, not a path: the destination is deleted
#                         before the move, and taking only a name means no
#                         caller-supplied value can ever name a directory
#                         outside dist/.
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
        # Ship the SPA outside the wheel. Databricks Apps uploads each source
        # file to the Workspace, which rejects files over 10 MB, and ~25 MB of
        # assets push the main wheel past that cap. As loose files every asset
        # is well under it; the server picks them up via OMNIGENT_WEB_UI_DIST.
        #
        # The destination is `rm -rf`'d below, so accept a bare name only and
        # anchor it under dist/ here. A caller-supplied *path* could escape the
        # repo through `..` or a symlink no matter how it were string-matched;
        # a name that cannot contain `/` or `.` has nowhere to escape to.
        if [[ ! "${WEB_UI_OUT_NAME}" =~ ^[A-Za-z0-9_-]+$ ]]; then
            echo "ERROR: WEB_UI_OUT_NAME must be a bare directory name matching" \
                 "[A-Za-z0-9_-]+ (got '${WEB_UI_OUT_NAME}')" >&2
            exit 1
        fi
        # dist/ was just removed and is recreated here, so it is a real
        # directory inside the repo rather than a pre-existing symlink.
        mkdir -p "${REPO_ROOT}/dist"
        web_ui_out_dir="${REPO_ROOT}/dist/${WEB_UI_OUT_NAME}"
        echo "==> Moving SPA out of the wheel into ${web_ui_out_dir}"
        rm -rf "${web_ui_out_dir}"
        mv omnigent/server/static/web-ui "${web_ui_out_dir}"
        # setup.py's build_py hook rebuilds the SPA into the package whenever
        # the bundle is missing, which would undo the move above and put the
        # assets straight back into the wheel. This script owns the SPA build,
        # so opt the backend out for the wheel builds below.
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
