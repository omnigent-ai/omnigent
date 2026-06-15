#!/usr/bin/env bash
# Iterates `REQUIRED` (defined in required.sh) against the actual
# check-runs on the PR head SHA. When GitHub has multiple check-runs
# with the same name on the same SHA (for example after re-running PR
# Template on an edited description), the newest run wins.
# Each check counts as green when:
#   - conclusion=success, OR
#   - conclusion=skipped AND name is in ALLOW_SKIP, OR
#   - the check is missing AND name is in ALLOW_SKIP AND its owning
#     workflow either never ran for this SHA (path-ignored), or its
#     newest run succeeded (the absent check was conditionally excluded
#     from that run's job matrix), or its newest run was skipped (the
#     whole workflow was gated off, e.g. a fork/draft PR) — see
#     workflow_run_outcome.
#
# A missing ALLOW_SKIP check is NOT green only while its workflow's
# newest run is still in flight / cancelled / failed: the check could
# still be pending or was lost, so the gate must wait. Inferring "skip"
# from mere absence let PR #2218 merge while an E2E shard was cancelled
# and re-running. Trusting a *succeeded* run keeps path-filtered jobs
# (e.g. CI's dynamically-selected Pytest shards on a docs/deploy-only
# PR) from blocking the gate; trusting a *skipped* run keeps fork/draft
# PRs — whose entire e2e workflow is gated off — from wedging it.
#
# Env in: GH_TOKEN, REPO, SHA
# Out:    failed=<markdown bullet list of failed names> on $GITHUB_OUTPUT
# Exit:   0 if all green, 1 if any red.

set -euo pipefail

HERE=$(dirname "$0")
# shellcheck disable=SC1091
source "$HERE/required.sh"

CHECKS=$(gh api "repos/$REPO/commits/$SHA/check-runs" --paginate \
  --jq '.check_runs[] | "\(.name)\t\(.status)\t\(.conclusion // "null")\t\(.completed_at // .started_at // "")"')

# Per-workflow run state for this SHA (one row per run:
# name<TAB>status<TAB>conclusion<TAB>created_at). Used to classify a
# *missing* required check via workflow_run_outcome below.
WORKFLOW_RUNS=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=100" --paginate \
  --jq '.workflow_runs[] | [.name, .status, (.conclusion // "null"), (.created_at // "")] | @tsv')

# Classify the newest run of a workflow for this SHA:
#   "none"    — no run at all. The workflow was gated out by
#               on.pull_request.paths-ignore, so its checks are
#               legitimately absent.
#   "success" — newest run completed successfully. A check that is still
#               absent was conditionally excluded from that run's job
#               matrix (e.g. CI dynamically path-filters its Pytest
#               shards); the green workflow vouches the job wasn't needed.
#   "skipped" — newest run completed with conclusion=skipped: every job's
#               `if:` was false, so the run did no work (e2e fork guard on
#               a fork PR, e2e-ui `!draft` on a draft PR). A definitive
#               skip, not a transient, so absent ALLOW_SKIP checks pass.
#   "other"   — in progress, queued, cancelled, or failed. An absent
#               check may still be pending or was lost, so the gate must
#               wait rather than treat the gap as a skip (the #2218 race,
#               where an E2E shard was cancelled and re-running at the
#               moment the gate evaluated).
workflow_run_outcome() {
  local wf="$1" row status concl
  row=$(printf '%s\n' "$WORKFLOW_RUNS" | awk -F'\t' -v w="$wf" '$1 == w' \
    | sort -t $'\t' -k4,4 | tail -n 1)
  if [[ -z "$row" ]]; then
    echo "none"
    return
  fi
  status=$(printf '%s' "$row" | cut -f2)
  concl=$(printf '%s' "$row" | cut -f3)
  if [[ "$status" == "completed" && "$concl" == "success" ]]; then
    echo "success"
  elif [[ "$status" == "completed" && "$concl" == "skipped" ]]; then
    echo "skipped"
  else
    echo "other"
  fi
}

FAIL=0
FAILED_LINES=""
for n in "${REQUIRED[@]}"; do
  ROW=$(echo "$CHECKS" | awk -F'\t' -v n="$n" '$1 == n {print}' | sort -t $'\t' -k4,4 | tail -n 1)
  if [[ -z "$ROW" ]]; then
    if is_allow_skip "$n"; then
      wf=$(workflow_for "$n")
      outcome="none"
      [[ -n "$wf" ]] && outcome=$(workflow_run_outcome "$wf")
      if [[ "$outcome" == "other" ]]; then
        echo "NOT GREEN: $n  (workflow '$wf' has not succeeded and the check is missing -- pending/cancelled, not a skip)"
        FAILED_LINES+="- \`$n\` (workflow ran but has not succeeded and the check is missing -- still pending or cancelled)"$'\n'
        FAIL=1
        continue
      fi
      # outcome is "none" (workflow path-skipped), "success" (job
      # conditionally excluded from a green run), or "skipped" (whole
      # workflow gated off, e.g. fork/draft PR) — all legitimate.
      echo "OK      : $n  (skipped: path-ignored, conditionally-excluded, or fork/draft-gated)"
      continue
    fi
    echo "MISSING : $n"
    FAILED_LINES+="- \`$n\` (not yet started or not configured on this commit)"$'\n'
    FAIL=1
    continue
  fi
  STATUS=$(echo "$ROW" | cut -f2)
  CONCL=$(echo "$ROW" | cut -f3)
  if [[ "$STATUS" != "completed" ]]; then
    echo "NOT GREEN: $n  (status=$STATUS, conclusion=$CONCL)"
    FAILED_LINES+="- \`$n\` (still running, status=$STATUS)"$'\n'
    FAIL=1
  elif [[ "$CONCL" == "skipped" ]] && is_allow_skip "$n"; then
    echo "OK      : $n  (skipped via path filter)"
  elif [[ "$CONCL" != "success" ]]; then
    echo "NOT GREEN: $n  (status=$STATUS, conclusion=$CONCL)"
    FAILED_LINES+="- \`$n\` (conclusion=$CONCL)"$'\n'
    FAIL=1
  else
    echo "OK      : $n"
  fi
done

{
  echo "failed<<_FAILED_EOF_"
  printf '%s' "$FAILED_LINES"
  echo "_FAILED_EOF_"
} >> "$GITHUB_OUTPUT"

if [[ $FAIL -eq 1 ]]; then
  echo ""
  echo "Required checks are not all green on $SHA."
  exit 1
fi

echo "All required checks green on $SHA."
