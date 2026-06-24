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
# Env in: EVAL, FAILED
# Out:    state, short_desc, long_desc on $GITHUB_OUTPUT

set -euo pipefail

if [[ "$EVAL" == "success" ]]; then
  STATE=success
  SHORT="All required checks green"
  LONG=":white_check_mark: gate is green, merging now."
else
  STATE=failure
  SHORT="Required checks not all green"
  LONG=$':hourglass: gate not green yet. Required checks not satisfied:\n\n'"$FAILED"$'\nThe merge will fire once these turn green.'
fi

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
