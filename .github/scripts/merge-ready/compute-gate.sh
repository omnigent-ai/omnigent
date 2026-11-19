#!/usr/bin/env bash
# Single source of truth for the Merge Ready outcome. Downstream steps
# just consume `state`, `short_desc`, and `long_desc`.
#
# The gate is green iff every required check is green on its own merits. There is
# no CI bypass: to land despite red required checks, fix or delete the failing
# test, or have a repo admin use GitHub's native "merge without waiting for
# requirements" affordance. (Fork PRs still need a maintainer's approving review
# to merge -- that is enforced by the separate `Maintainer Approval` check, not
# here. No CI suite needs secrets on a fork PR anymore, so there is no
# e2e-specific approval gate.)
#
#   CI eval  | state    | meaning
#   ---------+----------+----------------------------
#   success  | success  | all required checks green
#   failure  | failure  | a required check is red
#
# Env in: EVAL, FAILED, DEPENDENCY_EVAL, DEPENDENCY_REQUIRED, POST_RED,
#         FRESHNESS_FAILED, API_FAILED, EXPIRES_AT
# Out:    state, short_desc, long_desc, post_status on $GITHUB_OUTPUT

set -euo pipefail

FRESHNESS_FAILED=${FRESHNESS_FAILED:-false}
API_FAILED=${API_FAILED:-false}
EXPIRES_AT=${EXPIRES_AT:-}
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [[ -n "$EXPIRES_AT" && ( "$EXPIRES_AT" < "$NOW" || "$EXPIRES_AT" == "$NOW" ) ]]; then
  FRESHNESS_FAILED=true
  FAILED+=$'\n- CI expired during evaluation; update the branch or push a new commit for fresh CI.\n'
fi
DEPENDENCY_FAILED=false
if [[ "$DEPENDENCY_REQUIRED" != "false" && "$DEPENDENCY_EVAL" != "success" ]]; then
  DEPENDENCY_FAILED=true
  FAILED+=$'\n- Dependency policy/reports (see Evaluate dependency reports)\n'
fi

if [[ "$EVAL" == "success" && "$DEPENDENCY_FAILED" == "false" && "$FRESHNESS_FAILED" != "true" ]]; then
  STATE=success
  SHORT="All required checks green"
  [[ -z "$EXPIRES_AT" ]] || SHORT+="; expires $EXPIRES_AT"
  LONG=":white_check_mark: gate is green, merging now."
else
  STATE=failure
  SHORT="Required checks not all green"
  LONG=$':hourglass: gate not green yet. Required checks not satisfied:\n\n'"$FAILED"$'\nThe merge will fire once these turn green.'
  if [[ "$API_FAILED" == "true" ]]; then
    SHORT="GitHub API request failed; retry Merge Ready"
  elif [[ "$FRESHNESS_FAILED" == "true" ]]; then
    SHORT="CI freshness not satisfied (24h); update branch and run fresh CI"
  fi
fi

POST_STATUS=false
if [[ "$STATE" == "success" || "$POST_RED" == "true" || "$DEPENDENCY_FAILED" == "true" || "$FRESHNESS_FAILED" == "true" ]]; then
  POST_STATUS=true
fi
echo "post_status=$POST_STATUS" >> "$GITHUB_OUTPUT"

# GitHub commit-status descriptions max out at 140 chars.
if [[ ${#SHORT} -gt 140 ]]; then
  SHORT="${SHORT:0:137}..."
fi

echo "state=$STATE" >> "$GITHUB_OUTPUT"
echo "short_desc=$SHORT" >> "$GITHUB_OUTPUT"
{
  echo "long_desc<<_LONG_EOF_"
  printf '%s' "$LONG"
  echo
  echo "_LONG_EOF_"
} >> "$GITHUB_OUTPUT"
echo "Computed gate: state=$STATE | $SHORT"
