#!/usr/bin/env bash
# Regenerate the committed UI-snapshot baseline locally, using Docker.
#
# Visual baselines must be rendered in the SAME environment the CI gate uses, or
# they won't match (fonts/anti-aliasing differ across renderers). This script
# renders inside the exact digest-pinned Playwright image ui-snapshot.yml runs
# in, so the baseline it produces is byte-identical to what CI will compare
# against -- commit it directly.
#
# Only Docker is required (no local Node/Python/uv). It:
#   1. builds the ap-web SPA in a Node 20 container, then
#   2. compares the whole visual suite in the pinned Playwright image and
#      rewrites only the baselines that drift (or are missing) -- baselines that
#      already match are left byte-for-byte untouched, mirroring the label-driven
#      CI flow (installs the project + Chromium-from-the-image, no browser
#      download).
#
# Usage:
#   tests/e2e_ui/visual/regen_baseline_docker.sh [--skip-build]
#
#   --skip-build  Reuse an existing omnigent/server/static/web-ui build (e.g.
#                 from a prior `cd ap-web && npm run build`) instead of building
#                 in a container. The bundle is platform-independent, so a host
#                 build renders the same pixels.
set -euo pipefail

# Keep these in lockstep with ui-snapshot.yml / ui-snapshot-update.yml.
PW_IMAGE="mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384"
NODE_IMAGE="node:20-bookworm"
# The pinned digest is a multi-arch manifest, and CI renders the linux/amd64
# variant. Force it here too so an arm64 host (e.g. Apple Silicon) renders the
# same Chromium build -- otherwise the local PNG diverges from the gate. On
# arm64 this runs under emulation (slower; needs Docker's binfmt/qemu).
PLATFORM="linux/amd64"
# Match CI's npm pin (.github/actions/setup-node) so the SPA bundle the build
# produces is identical to the one the gate renders.
NPM_VERSION="11.12.1"
BUILD_OUTPUT="omnigent/server/static/web-ui"
SNAP_ROOT="tests/e2e_ui/visual/snapshots"

SKIP_BUILD=false
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) SKIP_BUILD=true; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown argument $1" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null || { echo "error: docker is required." >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"

if [ "$SKIP_BUILD" = true ]; then
  [ -f "$BUILD_OUTPUT/index.html" ] || {
    echo "error: --skip-build but no SPA build at $BUILD_OUTPUT. Build it first or drop the flag." >&2
    exit 1
  }
  echo "Reusing existing SPA build at $BUILD_OUTPUT."
else
  echo "Building the ap-web SPA (Node container) ..."
  docker run --rm --platform "$PLATFORM" -v "$PWD":/work -w /work/ap-web "$NODE_IMAGE" \
    bash -c "npm install -g npm@${NPM_VERSION} && npm ci --legacy-peer-deps --no-audit --no-fund && npm run build"
fi

echo "Rendering + comparing the baselines in the pinned Playwright image ..."
# Deliberately NOT --update-snapshots: that rewrites every PNG, churning
# baselines that still pass (a sub-threshold re-render changes the bytes). Plain
# compare leaves passing baselines alone and rewrites only the drift. GITHUB_ACTIONS
# is set so the plugin behaves exactly as the CI gate does: it updates a mismatching
# baseline IN PLACE (and creates a missing one) under snapshots/, so the git diff
# below is the real signal. The run "fails" by design on any drift, so `|| true` is
# expected. UV_PROJECT_ENVIRONMENT lives in the container (not the mounted repo) so
# no root-owned .venv leaks out.
docker run --rm --platform "$PLATFORM" -v "$PWD":/work -w /work \
  -e CI=1 \
  -e GITHUB_ACTIONS=true \
  -e OMNIGENT_PW_NO_SANDBOX=1 \
  -e OMNIGENT_SKIP_WEB_UI=true \
  -e UV_PYTHON_PREFERENCE=only-system \
  -e UV_PROJECT_ENVIRONMENT=/opt/uv-venv \
  "$PW_IMAGE" bash -c '
    pip install --quiet uv &&
    uv sync --extra all --extra dev &&
    uv run pytest tests/e2e_ui/visual -m visual \
      -p no:rerunfailures --ui-skip-build
  ' || true

# Files Docker wrote are root-owned; hand them back so git add works unprivileged.
# Includes ap-web (node_modules + build intermediates the Node container wrote).
docker run --rm --platform "$PLATFORM" -v "$PWD":/work "$PW_IMAGE" \
  chown -R "$(id -u):$(id -g)" /work/tests/e2e_ui/visual /work/"$BUILD_OUTPUT" /work/ap-web 2>/dev/null || true

echo
if git diff --quiet -- "$SNAP_ROOT"; then
  echo "Baselines unchanged — they already match this render (or the render failed; check the output above)."
else
  git --no-pager diff --stat -- "$SNAP_ROOT" || true
  echo
  echo "Updated baseline(s) under: $SNAP_ROOT"
  echo "Next: review the image(s), then commit + push:"
  echo "  git add \"$SNAP_ROOT\" && git commit -m 'test(ui-snapshot): update visual baselines' && git push"
fi
