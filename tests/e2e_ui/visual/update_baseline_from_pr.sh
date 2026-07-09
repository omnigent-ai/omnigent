#!/usr/bin/env bash
# Update the committed UI-snapshot baseline from a PR's CI-rendered artifact.
#
# The no-Docker fallback for fork PRs. Baselines must be rendered in the pinned
# Playwright image (screenshots differ across renderers); if you have Docker,
# prefer regen_baseline_docker.sh, which reproduces that render locally. Without
# Docker, the failing UI Snapshot run already rendered your change in that image,
# so this pulls its `actual_` PNG into the baseline for review + commit.
#
# Usage:
#   tests/e2e_ui/visual/update_baseline_from_pr.sh <pr-number> [--repo owner/name]
#
# Requires: gh (authenticated), git. The artifact is kept for 7 days.
set -euo pipefail

SNAP_ROOT="tests/e2e_ui/visual/snapshots"

PR=""
REPO="${REPO:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "error: unknown flag $1" >&2; exit 2 ;;
    *) PR="$1"; shift ;;
  esac
done

case "$PR" in
  "" | *[!0-9]*)
    echo "error: PR must be a number. Usage: $0 <pr-number> [--repo owner/name]" >&2
    exit 2
    ;;
esac

# Run from the repo root so the baseline path resolves regardless of CWD.
cd "$(git rev-parse --show-toplevel)"
[ -z "$REPO" ] && REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)

echo "Resolving PR #$PR in $REPO ..."
HEAD_SHA=$(gh pr view "$PR" --repo "$REPO" --json headRefOid -q .headRefOid)
echo "  head SHA: $HEAD_SHA"

# pull_request runs (incl. fork PRs) live in the base repo, keyed by head SHA.
RUN_ID=$(gh api "repos/$REPO/actions/runs?head_sha=$HEAD_SHA&per_page=100" \
  --jq '[.workflow_runs[] | select(.name=="UI Snapshot")] | sort_by(.created_at) | last | .id // empty')
if [ -z "$RUN_ID" ]; then
  echo "error: no 'UI Snapshot' run found for $HEAD_SHA -- has CI run on this PR's head?" >&2
  exit 1
fi
echo "  UI Snapshot run: $RUN_ID"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Downloading artifact ui-snapshot-$RUN_ID ..."
if ! gh run download "$RUN_ID" --repo "$REPO" -n "ui-snapshot-$RUN_ID" -D "$TMP" 2>/dev/null; then
  gh run download "$RUN_ID" --repo "$REPO" -D "$TMP"  # fall back to all artifacts
fi

# The gate runs under GitHub Actions, where the plugin rewrites a mismatching
# baseline IN PLACE under snapshots/ (and creates a missing one there); passing
# baselines are left untouched. The artifact carries that whole snapshots/ tree,
# so just restore it over the committed one -- git add then stages exactly the
# baselines that drifted. (Reconstructing paths from snapshot_failures/ instead
# is fragile: that tree keys subdirs by <test>[browser][platform], not the
# baseline's <test>, so it would write a parallel, never-read duplicate.)
src_root=$(find "$TMP" -type d -name snapshots | head -1)
if [ -z "$src_root" ]; then
  echo "error: artifact has no snapshots/ tree -- nothing to restore." >&2
  exit 1
fi
cp -R "$src_root/." "$SNAP_ROOT/"

if git diff --quiet -- "$SNAP_ROOT"; then
  echo "No baseline drift in the artifact -- the gate render already matches the committed baselines. Nothing to update." >&2
  exit 0
fi

echo
echo "Updated baseline(s) from the gate render:"
git --no-pager diff --stat -- "$SNAP_ROOT" || true
echo
echo "Next: review the image(s), then commit + push:"
echo "  git add \"$SNAP_ROOT\" && git commit -m 'test(ui-snapshot): update visual baselines' && git push"
