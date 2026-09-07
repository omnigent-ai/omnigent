#!/usr/bin/env bash
# Regenerate the committed UI-snapshot baseline locally using a container runtime.
#
# Visual baselines must be rendered in the SAME environment the CI gate uses, or
# they won't match (fonts/anti-aliasing differ across renderers). This script
# renders inside the exact digest-pinned Playwright image ui-snapshot.yml runs
# in, so the baseline it produces is byte-identical to what CI will compare
# against -- commit it directly.
#
# Only a container runtime is required (no local Node/Python/uv).  Set
# OMNIGENT_CONTAINER_RUNTIME=podman to use Podman instead of Docker.  It:
#   1. builds the web SPA and static Storybook in a Node 20 container, then
#   2. compares the whole visual suite in the pinned Playwright image and
#      rewrites only the baselines that drift (or are missing) -- baselines that
#      already match are left byte-for-byte untouched, mirroring the label-driven
#      CI flow (installs the project + Chromium-from-the-image, no browser
#      download).
#
# Usage:
#   tests/e2e_ui/visual/regen_baseline_docker.sh [--skip-build]
#
#   --skip-build  Reuse existing SPA and Storybook builds instead of building
#                 in a container. The bundles are platform-independent, so host
#                 builds render the same pixels.
set -euo pipefail

# Keep these in lockstep with ui-snapshot.yml / ui-snapshot-update.yml.
PW_IMAGE="mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384"
NODE_IMAGE="node:20-bookworm"
# The pinned digest is a multi-arch manifest, and CI renders the linux/amd64
# variant. Force it here too so an arm64 host (e.g. Apple Silicon) renders the
# same Chromium build -- otherwise the local PNG diverges from the gate. On
# arm64 this runs under emulation (slower; needs Docker's binfmt/qemu).
PLATFORM="linux/amd64"
# Match the workspace package-manager pin used by CI.
PNPM_VERSION="11.15.1"
BUILD_OUTPUT="omnigent/server/static/web-ui"
STORYBOOK_OUTPUT="web/storybook-static"
SNAP_ROOT="tests/e2e_ui/visual/snapshots"

SKIP_BUILD=false
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) SKIP_BUILD=true; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown argument $1" >&2; exit 2 ;;
  esac
done

CONTAINER_RUNTIME="${OMNIGENT_CONTAINER_RUNTIME:-docker}"
command -v "$CONTAINER_RUNTIME" >/dev/null || { echo "error: $CONTAINER_RUNTIME is required." >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"

if [ "$SKIP_BUILD" = true ]; then
  [ -f "$BUILD_OUTPUT/index.html" ] && [ -f "$STORYBOOK_OUTPUT/index.json" ] || {
    echo "error: --skip-build requires builds at $BUILD_OUTPUT and $STORYBOOK_OUTPUT." >&2
    exit 1
  }
  echo "Reusing existing SPA and Storybook builds."
else
  echo "Building the web SPA and Storybook (Node container) ..."
  "$CONTAINER_RUNTIME" run --rm --platform "$PLATFORM" -v "$PWD":/work -w /work "$NODE_IMAGE" \
    bash -c "npm install -g pnpm@${PNPM_VERSION} && pnpm install --frozen-lockfile --filter web && pnpm --filter web run build && pnpm --filter web run build:storybook"
fi

echo "Rendering + comparing the baselines in the pinned Playwright image ..."
# Deliberately NOT --update-snapshots: that rewrites every PNG, churning
# baselines that still pass (a sub-threshold re-render changes the bytes). Plain
# compare leaves passing baselines alone and rewrites only the drift. GITHUB_ACTIONS
# is set so the plugin behaves exactly as the CI gate does: it updates a mismatching
# baseline IN PLACE (and creates a missing one) under snapshots/. The first run may
# fail by design on drift; the second must pass, separating baseline updates from
# render failures. UV_PROJECT_ENVIRONMENT lives in the container (not the mounted
# repo) so no root-owned .venv leaks out.
RENDER_FAILED=false
if ! "$CONTAINER_RUNTIME" run --rm --platform "$PLATFORM" -v "$PWD":/work -w /work \
  -e CI=1 \
  -e GITHUB_ACTIONS=true \
  -e OMNIGENT_PW_NO_SANDBOX=1 \
  -e OMNIGENT_SKIP_WEB_UI=true \
  -e UV_PYTHON_PREFERENCE=only-system \
  -e UV_PROJECT_ENVIRONMENT=/opt/uv-venv \
  "$PW_IMAGE" bash -c '
    pip install --quiet uv &&
    uv sync --extra all --group test &&
    (uv run pytest tests/e2e_ui/visual -m visual \
      -p no:rerunfailures --ui-skip-build || true) &&
    uv run pytest tests/e2e_ui/visual -m visual \
      -p no:rerunfailures --ui-skip-build
  '; then
  RENDER_FAILED=true
fi

# Files the container wrote are root-owned; hand them back so git add works unprivileged.
# Includes web (node_modules + build intermediates the Node container wrote).
"$CONTAINER_RUNTIME" run --rm --platform "$PLATFORM" -v "$PWD":/work "$PW_IMAGE" \
  chown -R "$(id -u):$(id -g)" /work/tests/e2e_ui/visual /work/"$BUILD_OUTPUT" /work/web 2>/dev/null || true

echo
if [ "$RENDER_FAILED" = true ]; then
  echo "error: the verification render failed; review the output above." >&2
  exit 1
fi
if [ -z "$(git status --porcelain -- "$SNAP_ROOT")" ]; then
  echo "Baselines unchanged — they already match this render."
else
  git status --short -- "$SNAP_ROOT"
  echo
  echo "Updated baseline(s) under: $SNAP_ROOT"
  echo "Next: review the image(s), then commit + push:"
  echo "  git add \"$SNAP_ROOT\" && git commit -m 'test(ui-snapshot): update visual baselines' && git push"
fi
