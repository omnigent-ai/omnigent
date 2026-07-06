#!/usr/bin/env bash
# Sync this fork with upstream, replay the local patch set, and refresh the
# local runtime so the synced code is actually live.
#
#   main  = pristine mirror of upstream/main (fast-forward only)
#   mine  = main + cherry-picked PRs; rebased onto fresh upstream each run
#
# A dirty working tree is auto-stashed before the sync and restored after, so
# in-progress edits survive. Runs against the repo this script lives in,
# regardless of the current directory.
#
# The server restart is deliberately NOT done here when running inside an
# omnigent session (OMNIGENT_RUNNER_ID set) — restarting the server would kill
# the very session running this script. In that case the caller (the launchd
# auto-sync orchestrator, or the user from a normal shell) owns the restart.
#
# Usage: .claude/skills/sync-fork/sync-fork.sh
set -euo pipefail

UPSTREAM=upstream      # remote -> omnigent-ai/omnigent
FORK=origin            # remote -> your fork
MIRROR=main            # pristine upstream mirror branch
PATCHED=mine           # integration branch carrying your cherry-picks
LAUNCHD_LABEL=dev.zarz.omnigent   # launchd agent running the local server

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

start_branch=$(git rev-parse --abbrev-ref HEAD)

stashed=0
if [ -n "$(git status --porcelain)" ]; then
  echo "==> stashing dirty working tree"
  git stash push -u -m "sync-fork autostash" >/dev/null
  stashed=1
fi

restore() {
  git checkout -q "$start_branch" 2>/dev/null || true
  if [ "$stashed" = 1 ]; then
    echo "==> restoring stashed working tree"
    git stash pop || echo "!! stash pop conflicted — your WIP is safe in 'git stash list'; resolve manually" >&2
  fi
}
trap restore EXIT

# push is non-fatal, and skipped inside an omnigent runner — a runner has no
# git credentials (no keychain, no user/agent context), so pushing there always
# fails. Inside omnigent the external orchestrator (auto-sync.sh) pushes instead.
push() {
  if [ -n "${OMNIGENT_RUNNER_ID:-}" ]; then
    echo "==> inside an omnigent session — deferring push ($*) to the orchestrator"
    return 0
  fi
  git push "$@" || echo "!! push failed ($*) — sync applied locally, fork remote not updated" >&2
}

echo "==> fetching $UPSTREAM"
git fetch "$UPSTREAM"

prev_upstream=$(git rev-parse -q --verify "$MIRROR" || echo "")

echo "==> fast-forwarding $MIRROR to $UPSTREAM/$MIRROR"
git branch -f "$MIRROR" "$UPSTREAM/$MIRROR"
push "$FORK" "$MIRROR"

echo "==> rebasing $PATCHED onto $UPSTREAM/$MIRROR"
git checkout -q "$PATCHED"
git rebase "$UPSTREAM/$MIRROR"
push --force-with-lease "$FORK" "$PATCHED"

# --- refresh the local runtime so synced code is actually live -----------
# The editable install runs Python straight from this checkout, but: the web
# UI is a prebuilt artifact that must be re-bundled, new deps must be synced,
# and the already-running server has the OLD modules imported until restarted.
new_upstream=$(git rev-parse "$MIRROR")

if [ -f web/package.json ] && \
   { [ -z "$prev_upstream" ] || ! git diff --quiet "$prev_upstream" "$new_upstream" -- web/; }; then
  echo "==> web UI changed — rebuilding the SPA bundle"
  # npm ci fails hard when upstream ships a package.json/lock out of sync;
  # fall back to npm install so an unattended sync self-heals the lock.
  ( cd web && { npm ci || npm install; } && npm run build )
else
  echo "==> web UI unchanged — skipping rebuild"
fi

echo "==> re-syncing the editable install (deps + version stamp)"
uv tool install --editable . --reinstall -q

# Restart only from OUTSIDE an omnigent session. Inside one, restarting the
# server kills this session mid-run; the external orchestrator restarts instead.
if [ -n "${OMNIGENT_RUNNER_ID:-}" ]; then
  echo "==> inside an omnigent session — skipping server restart (caller owns it)"
elif launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
  echo "==> restarting the launchd server to load the new code"
  launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"
  # Return only once the server is back, so the next command never sees it
  # mid-restart (the omnigent policy hook fails closed while it's down).
  port=$(sed -n 's/^local_server_port: *//p' "$HOME/.omnigent/config.yaml" 2>/dev/null)
  port=${port:-6767}
  echo "==> waiting for the server on :$port"
  for _ in $(seq 1 40); do
    curl -fsS --max-time 1 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
else
  echo "==> launchd agent $LAUNCHD_LABEL not loaded — skipping server restart"
fi

echo "==> done. $MIRROR and $PATCHED up to date; local runtime refreshed."
