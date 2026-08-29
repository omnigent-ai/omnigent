#!/usr/bin/env bash
# Install or update the omnigent nightly build.
#
# Nightlies are datestamped prerelease tags (vX.Y.Z.devYYYYMMDD) cut from
# the newest green commit on main by .github/workflows/nightly-release.yml
# (about 04:30 UTC). This script resolves the newest nightly tag and
# installs it with uv, which pins the whole install (omnigent,
# omnigent-client, omnigent-ui-sdk) to that one tagged commit.
#
# Requirements: git, uv, and Node.js 22+ with pnpm (the wheel build
# compiles the web UI and fails with an actionable message if they are
# missing).
#
# Idempotent: exits fast when the newest nightly is already installed,
# so it is safe to run from cron, e.g. daily at 9am:
#   0 9 * * * bash /path/to/update_nightly.sh >> "$HOME/.omnigent-nightly.log" 2>&1

set -euo pipefail

REPO="${OMNIGENT_REPO:-https://github.com/omnigent-ai/omnigent}"
# Match install.sh: pinning the interpreter keeps uv reusing the existing
# tool environment instead of recreating it (which removes the working
# install before the new wheel is built, so a build failure leaves none).
PYTHON_VERSION="${OMNIGENT_PYTHON_VERSION:-3.12}"

# Newest nightly tag: strictly vX.Y.Z.devYYYYMMDD (the 8-digit date also
# screens out legacy .dev0-style tags). Version sorts before date, so the
# first nightly after a main version bump outranks all older ones.
tag=$(git ls-remote --tags --sort=-v:refname "$REPO" 'refs/tags/v*.dev*' \
  | awk -F'refs/tags/' '{print $2}' \
  | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]{8}$' \
  | sed -n 1p)
if [ -z "$tag" ]; then
  echo "update_nightly: no nightly tags found in ${REPO}" >&2
  exit 1
fi

# Skip the (slow, web-UI-building) reinstall when already current.
if command -v omnigent >/dev/null 2>&1 \
  && omnigent --version 2>/dev/null | grep -qF "${tag#v}"; then
  echo "update_nightly: already on ${tag}"
  exit 0
fi

echo "update_nightly: installing ${tag}"
uv tool install --reinstall --python "$PYTHON_VERSION" \
  "omnigent @ git+${REPO}@${tag}"
omnigent --version
