#!/usr/bin/env bash
# Decides whether a fork PR's head commit should be mirrored onto the trusted
# fork-e2e/pr-N branch (which lets e2e run as a `push` with the test-gateway
# secrets). Called by .github/workflows/fork-e2e-mirror.yml.
#
# Gate (either condition opens it):
#   1. The PR has an approving review from a maintainer (in
#      .github/MAINTAINER@main), OR
#   2. The PR carries the `e2e-approved` label applied by a maintainer.
#
# Path 1 (approval) is the primary flow: approving the PR both satisfies the
# merge gate and triggers e2e. Path 2 (label) is a manual escape hatch for
# running e2e without approving for merge (e.g. early CI validation).
#
# New commits while the gate is open re-mirror automatically (this script
# re-runs on `synchronize`); the security scan plus the maintainer's review
# are the safety net for post-approval pushes. Revoking approval AND removing
# the label (or closing the PR) stops future mirrors and cleans up the mirror
# branch -- see the workflow.
#
# Fail closed: any error or unexpected state leaves the gate shut, so secrets
# never run on an unverified PR.
#
# Env in:  GH_TOKEN, REPO, PR,
#          MAINTAINERS (space-separated, from merge-ready/load-maintainers.sh).
# Out:     `mirror=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

emit() {
  echo "mirror=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "mirror=$1 ($2)"
}

MAINTAINERS_LC=$(echo "${MAINTAINERS:-}" | tr '[:upper:]' '[:lower:]')

if [[ -z "${MAINTAINERS_LC// /}" ]]; then
  emit false "no maintainers loaded (.github/MAINTAINER@main empty/missing)"
  exit 0
fi

# --- Path 1: maintainer approval via PR review ---

APPROVERS=$(gh api "repos/$REPO/pulls/$PR/reviews" --paginate \
  --jq '[.[] | select(.state != "COMMENTED")] | group_by(.user.login) | map(max_by(.submitted_at)) | .[] | select(.state == "APPROVED") | .user.login')

for u in $APPROVERS; do
  u_lc=$(echo "$u" | tr '[:upper:]' '[:lower:]')
  for m in $MAINTAINERS_LC; do
    if [[ "$m" == "$u_lc" ]]; then
      emit true "approved by maintainer @$u"
      exit 0
    fi
  done
done

# --- Path 2: e2e-approved label applied by a maintainer ---

LABEL="e2e-approved"
LABELS=$(gh pr view "$PR" --repo "$REPO" --json labels --jq '.labels[].name')
if grep -qxF "$LABEL" <<<"$LABELS"; then
  LABELER=$(gh api "repos/$REPO/issues/$PR/events" --paginate \
    --jq "[.[] | select(.event == \"labeled\" and .label.name == \"$LABEL\")] | last | .actor.login // empty")

  if [[ -n "$LABELER" ]]; then
    LABELER_LC=$(echo "$LABELER" | tr '[:upper:]' '[:lower:]')
    for m in $MAINTAINERS_LC; do
      if [[ "$m" == "$LABELER_LC" ]]; then
        emit true "'$LABEL' applied by maintainer @$LABELER"
        exit 0
      fi
    done
  fi
fi

# Neither path opened the gate.
emit false "awaiting approval from a maintainer or '$LABEL' label"
