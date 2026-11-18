#!/usr/bin/env bash
# Iterates `REQUIRED` (defined in required.sh) against the actual
# check-runs on the PR head SHA. When GitHub has multiple check-runs
# with the same name on the same SHA (for example after re-running PR
# Template on an edited description), the highest check-run ID wins, including
# queued retries without timestamps.
# Each check counts as green when:
#   - conclusion=success from a workflow created less than 24 hours ago, OR
#   - conclusion=skipped AND name is in ALLOW_SKIP and isn't a result check, OR
#   - the check is missing AND name is in ALLOW_SKIP AND its owning
#     workflow either never ran for this SHA (path-ignored), or its
#     newest run succeeded (the absent check was conditionally excluded
#     from that run's job matrix), or its newest run was skipped (the
#     whole workflow was gated off, e.g. a fork/draft PR) — see
#     workflow_run_outcome.
# Result checks must be present once their selection job exists; only older
# workflow versions may omit them.
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
# DCO never expires; reruns keep the original workflow created_at.
# Out: failed, freshness_failed, api_failed, expires_at on $GITHUB_OUTPUT.
# expires_at is the earliest successful CI deadline (UTC ISO8601).
# A check can still read as running for a while after its workflow run
# completes. When only such checks keep the gate red, re-read them a few
# times first: nothing else re-evaluates the gate once CI is done.
#
# Env in: GH_TOKEN, REPO, SHA; optional MERGE_READY_LAG_ATTEMPTS (default 6)
#         and MERGE_READY_LAG_DELAY_S (default 20) bound that re-read.
# Exit:   0 if all green, 1 if any red.

set -euo pipefail

HERE=$(dirname "$0")
# shellcheck disable=SC1091
source "$HERE/required.sh"

LAG_ATTEMPTS=${MERGE_READY_LAG_ATTEMPTS:-6}
LAG_DELAY_S=${MERGE_READY_LAG_DELAY_S:-20}
MAX_AGE_SECONDS=$((24 * 60 * 60))

# Check runs, plus per-workflow run state for this SHA (one row per run:
# name<TAB>status<TAB>conclusion<TAB>created_at<TAB>suite_id<TAB>created_epoch).
# The run state classifies a missing required check via workflow_run_outcome.
fetch_state() {
  CHECKS=$(gh api "repos/$REPO/commits/$SHA/check-runs" --paginate \
    --jq '.check_runs[] | [.name, .status, (.conclusion // "null"), .id, (.check_suite.id // "")] | @tsv') || return 1
  WORKFLOW_RUNS=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=100" --paginate \
    --jq '.workflow_runs[] | .created_at as $created |
      [.name, .status, (.conclusion // "null"), ($created // ""), (.check_suite_id // ""),
       ((try ($created | fromdateiso8601 | select(todateiso8601 == $created)) catch null) // -1)] | @tsv') || return 1
}

# Match the selected check itself, rather than a newer unrelated run of its workflow.
workflow_run_for_check() {
  local suite_id="${1##*$'\t'}"
  [[ "$suite_id" =~ ^[1-9][0-9]*$ ]] || return 0
  printf '%s\n' "$WORKFLOW_RUNS" | awk -F'\t' -v suite_id="$suite_id" '$5 == suite_id'
}

require_fresh_workflow() {
  local name="$1" row="$2" created_epoch deadline reason
  created_epoch="${row##*$'\t'}"
  if [[ ! "$created_epoch" =~ ^[0-9]+$ ]] || [[ "$created_epoch" -gt "$NOW" ]]; then
    reason="CI age could not be verified; update the branch to run fresh CI"
  elif [[ $((NOW - created_epoch)) -ge $MAX_AGE_SECONDS ]]; then
    reason="CI is at least 24 hours old; update the branch to run fresh CI"
  else
    deadline=$((created_epoch + MAX_AGE_SECONDS))
    if [[ -z "$EXPIRES_AT_EPOCH" || "$deadline" -lt "$EXPIRES_AT_EPOCH" ]]; then
      EXPIRES_AT_EPOCH=$deadline
    fi
    return 0
  fi
  echo "NOT GREEN: $name  ($reason)"
  FAILED_LINES+="- \`$name\` ($reason)"$'\n'
  FAIL=1
  ONLY_LAGGING=0
  FRESHNESS_FAILED=true
  return 1
}

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
newest_workflow_run() {
  printf '%s\n' "$WORKFLOW_RUNS" | awk -F'\t' -v w="$1" '$1 == w' \
    | sort -t $'\t' -k4,4 | tail -n 1
}

workflow_run_outcome() {
  local row="$1" _workflow_name status concl _remainder
  if [[ -z "$row" ]]; then
    echo "none"
    return
  fi
  IFS=$'\t' read -r _workflow_name status concl _remainder <<<"$row"
  if [[ "$status" == "completed" && "$concl" == "success" ]]; then
    echo "success"
  elif [[ "$status" == "completed" && "$concl" == "skipped" ]]; then
    echo "skipped"
  else
    echo "other"
  fi
}

# Whether the newest run of a workflow for this SHA has completed, whatever
# its conclusion.
workflow_run_finished() {
  newest_workflow_run "$1" | awk -F'\t' '$2 == "completed" {found=1} END {exit !found}'
}

# Sets FAIL, FAILED_LINES, and ONLY_LAGGING (every red check is one that
# still reads as running although its workflow run has completed).
evaluate_once() {
  FAIL=0
  FAILED_LINES=""
  ONLY_LAGGING=1
  FRESHNESS_FAILED=false
  EXPIRES_AT_EPOCH=""
  NOW=$(date -u +%s)
  for n in "${REQUIRED[@]}"; do
    ROW=$(echo "$CHECKS" | awk -F'\t' -v n="$n" '$1 == n {print}' | sort -t $'\t' -k4,4n | tail -n 1)
    scope=$(scope_for_result "$n")
    if [[ -z "$ROW" ]]; then
      if [[ -n "$scope" ]] && echo "$CHECKS" | awk -F'\t' -v names="$scope" 'BEGIN {split(names, scopes, ",")} {for (i in scopes) if ($1 == scopes[i]) found=1} END {exit !found}'; then
        echo "MISSING : $n  (selection job '$scope' requires an executed result check)"
        FAILED_LINES+="- \`$n\` (selection ran; waiting for its result check)"$'\n'
        FAIL=1
        ONLY_LAGGING=0
        continue
      fi
      if is_allow_skip "$n"; then
        wf=$(workflow_for "$n")
        workflow_row=""
        [[ -n "$wf" ]] && workflow_row=$(newest_workflow_run "$wf")
        outcome=$(workflow_run_outcome "$workflow_row")
        if [[ "$outcome" == "other" ]]; then
          echo "NOT GREEN: $n  (workflow '$wf' has not succeeded and the check is missing -- pending/cancelled, not a skip)"
          FAILED_LINES+="- \`$n\` (workflow ran but has not succeeded and the check is missing -- still pending or cancelled)"$'\n'
          FAIL=1
          ONLY_LAGGING=0
          continue
        fi
        if [[ "$outcome" == "success" ]] && ! require_fresh_workflow "$n" "$workflow_row"; then
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
      ONLY_LAGGING=0
      continue
    fi
    STATUS=$(echo "$ROW" | cut -f2)
    CONCL=$(echo "$ROW" | cut -f3)
    if [[ "$STATUS" != "completed" ]]; then
      wf=$(workflow_for "$n")
      if [[ -n "$wf" ]] && workflow_run_finished "$wf"; then
        echo "NOT GREEN: $n  (status=$STATUS although workflow '$wf' has completed)"
      else
        echo "NOT GREEN: $n  (status=$STATUS, conclusion=$CONCL)"
        ONLY_LAGGING=0
      fi
      FAILED_LINES+="- \`$n\` (still running, status=$STATUS)"$'\n'
      FAIL=1
    elif [[ "$CONCL" == "skipped" && -z "$scope" ]] && is_allow_skip "$n"; then
      echo "OK      : $n  (skipped via path filter)"
    elif [[ "$CONCL" != "success" ]]; then
      echo "NOT GREEN: $n  (status=$STATUS, conclusion=$CONCL)"
      FAILED_LINES+="- \`$n\` (conclusion=$CONCL)"$'\n'
      FAIL=1
      ONLY_LAGGING=0
    elif [[ "$n" == "DCO" ]] || require_fresh_workflow "$n" "$(workflow_run_for_check "$ROW")"; then
      echo "OK      : $n"
    fi
  done
}

API_FAILED=false
attempt=1
while true; do
  if ! fetch_state; then
    FAIL=1
    FRESHNESS_FAILED=true
    API_FAILED=true
    EXPIRES_AT_EPOCH=""
    FAILED_LINES="- CI freshness could not be verified (GitHub API request failed; retry Merge Ready)."$'\n'
    break
  fi
  evaluate_once
  if [[ $FAIL -eq 0 || $ONLY_LAGGING -eq 0 || $attempt -ge $LAG_ATTEMPTS ]]; then
    break
  fi
  echo ""
  echo "Only checks of completed workflow runs read as running; re-reading in ${LAG_DELAY_S}s (attempt $attempt/$LAG_ATTEMPTS)."
  sleep "$LAG_DELAY_S"
  attempt=$((attempt + 1))
done

{
  echo "freshness_failed=$FRESHNESS_FAILED"
  echo "api_failed=$API_FAILED"
  if [[ -n "$EXPIRES_AT_EPOCH" ]]; then
    echo "expires_at=$(jq -nr --argjson deadline "$EXPIRES_AT_EPOCH" '$deadline | todateiso8601')"
  else
    echo "expires_at="
  fi
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
