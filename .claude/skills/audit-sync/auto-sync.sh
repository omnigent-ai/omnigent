#!/usr/bin/env bash
# Daily audited auto-sync — launchd entrypoint.
#
# Runs OUTSIDE any omnigent session, so it can safely restart the server that
# the audit session runs under. Flow:
#
#   1. Detect the upstream delta. None -> done (never wake an agent for nothing).
#   2. Drive a headless omnigent claude session to audit the delta and, if the
#      audit is clean, apply it via sync-fork.sh (which auto-skips the restart
#      because it runs inside omnigent).
#   3. If `mine` advanced (i.e. the audit passed and it applied), restart the
#      server HERE to load the new backend. Held / no-op -> no restart.
#
# The audit gates the apply; this script only owns detection + the external
# restart. Everything else lives in the audit-sync + sync-fork skills.
set -uo pipefail

REPO="$HOME/dev/ext/omnigent/omnigent"
LABEL="dev.zarz.omnigent"
PORT=6767
LOGDIR="$HOME/.omnigent/logs/auto-sync"
mkdir -p "$LOGDIR"
STAMP=$(date +%Y%m%d-%H%M%S)
REPORT="$LOGDIR/report-$STAMP.md"

exec >>"$LOGDIR/$STAMP.log" 2>&1
echo "=== auto-sync $STAMP ==="

cd "$REPO" || { echo "repo not found: $REPO"; exit 1; }

# single-flight: never let a slow run stack on the next tick. Self-healing —
# a hard kill (SIGKILL/SIGQUIT, or timeout's SIGTERM) skips the EXIT trap and
# leaves the lock; the next run reclaims it when its pid is dead, so one killed
# run can't wedge the daily job forever.
LOCK="$LOGDIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then
    echo "another run in progress (pid $(cat "$LOCK/pid")) — exiting"; exit 0
  fi
  echo "reclaiming stale lock"; rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { echo "lock race — exiting"; exit 0; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK" 2>/dev/null || true' EXIT INT TERM

health() { python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$PORT/health',timeout=2).read() else 1)" 2>/dev/null; }
notify() { osascript -e "display notification \"$1\" with title \"omnigent auto-sync\"" 2>/dev/null || true; }

git fetch upstream -q || { echo "fetch failed"; exit 1; }
BASE=$(git merge-base mine upstream/main)
NEW=$(git rev-parse upstream/main)
if [ "$BASE" = "$NEW" ]; then
  echo "up to date ($NEW) — nothing to do"
  exit 0
fi
COUNT=$(git rev-list --count "$BASE..$NEW")
echo "delta $BASE..$NEW ($COUNT upstream commits) — auditing"

BEFORE=$(git rev-parse mine)

PROMPT="You are the omnigent fork's daily audited-sync agent. Repo: $REPO.
Follow .claude/skills/audit-sync/SKILL.md. Do this:
1. cd $REPO. git fetch upstream -q. BASE=\$(git merge-base mine upstream/main); NEW=\$(git rev-parse upstream/main). If BASE == NEW, write 'up to date' and stop.
2. Audit the upstream commits in BASE..NEW (git log --stat \$BASE..\$NEW; git diff \$BASE..\$NEW; git show for anything suspicious). If the update-delta-audit skill is available, use it. Flag HIGH severity for any of: new or widened network egress, telemetry / phone-home, risky new or bumped dependencies (pyproject.toml, uv.lock, web/package.json), auth / permission / sandbox changes, code that executes at import or install time, or breaking changes to the server/CLI paths this setup relies on.
3. Decide: if there is NO high-severity finding, APPLY by running: bash .claude/skills/sync-fork/sync-fork.sh  (it rebases, rebuilds the UI, reinstalls, and intentionally SKIPS the server restart inside omnigent; push is non-fatal). If there is ANY high-severity finding, or the audit is inconclusive, do NOT apply — HOLD.
4. Write a concise markdown report to $REPORT summarising the delta and findings, ending with a final line that is EXACTLY 'VERDICT: PASS' or 'VERDICT: HOLD <one-line reason>'.
Do NOT restart the omnigent server — the scheduler does that."

echo "--- driving headless omnigent session (<=15m) ---"
timeout 900 omnigent run --harness claude --tools coding -p "$PROMPT" < /dev/null
echo "--- session returned (exit $?) ---"

AFTER=$(git rev-parse mine)
[ -f "$REPORT" ] && { echo "--- report ---"; cat "$REPORT"; }

if [ "$BEFORE" != "$AFTER" ]; then
  echo "mine advanced $BEFORE -> $AFTER"
  # Push here (external context has the deploy key; the runner does not).
  echo "pushing main + mine to origin"
  git push origin main || echo "!! push main failed"
  git push --force-with-lease origin mine || echo "!! push mine failed"
  echo "restarting server to load backend"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  for _ in $(seq 1 40); do health && break; sleep 0.5; done
  if health; then
    echo "server healthy on :$PORT"
    notify "synced -> $(git rev-parse --short mine); server restarted"
  else
    echo "server DID NOT come back — check manually"
    notify "sync applied but server unhealthy — check logs"
  fi
else
  echo "mine unchanged — audit held or no-op; no restart"
  notify "held / no change — see report-$STAMP.md"
fi
echo "=== done ==="
