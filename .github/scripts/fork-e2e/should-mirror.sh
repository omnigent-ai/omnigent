#!/usr/bin/env bash
# Decides whether a fork PR's head commit should be mirrored onto the trusted
# fork-e2e/pr-N branch (which lets e2e run as a `push` with the test-gateway
# secrets). Called by .github/workflows/fork-e2e-mirror.yml.
#
# Gate: the PR currently carries the `e2e-approved` label AND that label was
# last applied by a maintainer (in .github/MAINTAINER@main). GitHub only lets
# Triage+ users apply labels, so an external fork author can never apply it; the
# maintainer check further narrows "anyone with Triage" down to the MAINTAINER
# list. We read the *labeler* from the issue-events timeline rather than the
# event sender, so the check still holds on `synchronize` (where the sender is
# the fork author pushing new commits, not the maintainer who labeled earlier).
#
# The label is intentionally separate from the merge gate (maintainer-approval.yml):
# labeling runs e2e but does NOT approve the PR for merge, and approving for
# merge does NOT run e2e. New commits while the label is present re-mirror
# automatically (this script re-runs on `synchronize`); the security scan plus
# the maintainer's review are the safety net for post-approval pushes. Removing
# the label (or closing the PR) deletes the mirror branch -- see the workflow.
#
# Fail closed: any error or unexpected state leaves the gate shut, so secrets
# never run on an unverified PR.
#
# Env in:  GH_TOKEN, REPO, PR, LABEL (gate label name, default e2e-approved),
#          MAINTAINERS (space-separated, from merge-ready/load-maintainers.sh).
# Out:     `mirror=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

emit() {
  echo "mirror=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "mirror=$1 ($2)"
}

LABEL="${LABEL:-e2e-approved}"
MAINTAINERS_LC=$(echo "${MAINTAINERS:-}" | tr '[:upper:]' '[:lower:]')

if [[ -z "${MAINTAINERS_LC// /}" ]]; then
  emit false "no maintainers loaded (.github/MAINTAINER@main empty/missing)"
  exit 0
fi

# 1. Label currently present? Read into a variable first so grep's early exit
#    can't SIGPIPE the producer, then match against a here-string.
LABELS=$(gh pr view "$PR" --repo "$REPO" --json labels --jq '.labels[].name')
if ! grep -qxF "$LABEL" <<<"$LABELS"; then
  emit false "awaiting '$LABEL' label from a maintainer"
  exit 0
fi

# 2. Who applied it last? Latest `labeled` event for this label on the timeline.
#    (Re-applying after a removal makes the most recent labeler authoritative.)
LABELER=$(gh api "repos/$REPO/issues/$PR/events" --paginate \
  --jq "[.[] | select(.event == \"labeled\" and .label.name == \"$LABEL\")] | last | .actor.login // empty")

if [[ -z "$LABELER" ]]; then
  # Label is present but no labeled event found (e.g. created with the PR via a
  # template) -- can't attribute it to a maintainer, so stay shut.
  emit false "'$LABEL' present but no attributable labeler; treating as ungated"
  exit 0
fi

LABELER_LC=$(echo "$LABELER" | tr '[:upper:]' '[:lower:]')
for m in $MAINTAINERS_LC; do
  if [[ "$m" == "$LABELER_LC" ]]; then
    emit true "'$LABEL' applied by maintainer @$LABELER"
    exit 0
  fi
done

emit false "'$LABEL' applied by non-maintainer @$LABELER; ignoring"
