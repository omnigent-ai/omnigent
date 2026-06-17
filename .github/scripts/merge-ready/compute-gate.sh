#!/usr/bin/env bash
# Single source of truth for the Merge Ready outcome. Downstream steps
# just consume `state`, `short_desc`, and `long_desc`.
#
# The gate is green iff every required check is green on its own merits.
# There is no CI bypass: to land despite red required checks, quarantine the
# flaky test (tests/known_failures.yaml) or have a repo admin use GitHub's
# native "merge without waiting for requirements" affordance.
#
#   CI eval  | state    | meaning
#   ---------+----------+---------------------------
#   success  | success  | CI green on its own merits
#   failure  | failure  | CI red
#
# Env in: EVAL, FAILED, FORK_NEEDS_E2E_LABEL (optional, default false)
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

# Fork PRs never run e2e on their own: the fork `pull_request` run resolves to
# an empty shard matrix, so the suite only runs once a maintainer applies the
# `e2e-approved` label (which mirrors the head to a trusted fork-e2e/** branch).
# Without it the e2e checks are satisfied-via-skip and the PR can go green with
# e2e never having executed -- so nudge a maintainer to apply the label. Appended
# to the comment only (long_desc); short_desc is the 140-char commit status.
if [[ "${FORK_NEEDS_E2E_LABEL:-false}" == "true" ]]; then
  LONG="$LONG"$'\n\n:information_source: e2e tests do not run automatically on fork PRs. A maintainer can apply the `e2e-approved` label to run the full e2e suite against this PR.'
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
