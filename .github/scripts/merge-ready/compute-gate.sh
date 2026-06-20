#!/usr/bin/env bash
# Single source of truth for the Merge Ready outcome. Downstream steps
# just consume `state`, `short_desc`, and `long_desc`.
#
# The gate is green iff every required check is green on its own merits
# AND (for fork PRs) a maintainer has approved. There is no CI bypass: to
# land despite red required checks, quarantine the flaky test
# (tests/known_failures.yaml) or have a repo admin use GitHub's native
# "merge without waiting for requirements" affordance.
#
#   CI eval  | fork approval | state    | meaning
#   ---------+---------------+----------+---------------------------------
#   success  | n/a or true   | success  | CI green on its own merits
#   success  | false         | failure  | fork PR awaiting maintainer approval
#   failure  | any           | failure  | CI red
#
# Env in: EVAL, FAILED, FORK_NEEDS_E2E_APPROVAL (optional, default false)
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
# an empty shard matrix, so the suite only runs once a maintainer approves the
# PR (which mirrors the head to a trusted fork-e2e/** branch). Without approval
# the e2e checks are satisfied-via-skip and the PR would go green with e2e never
# having executed -- so block merge until a maintainer approves.
if [[ "${FORK_NEEDS_E2E_APPROVAL:-false}" == "true" ]]; then
  STATE=failure
  SHORT="Awaiting maintainer approval for e2e"
  LONG="$LONG"$'\n\n:no_entry: **E2e tests are required for fork PRs.** A maintainer must approve this PR or apply the `e2e-approved` label to trigger the e2e suite. The merge gate will stay red until e2e passes.'
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
