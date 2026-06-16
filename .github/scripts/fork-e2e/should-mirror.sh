#!/usr/bin/env bash
# Decides whether a fork PR's head commit should be mirrored onto the trusted
# fork-e2e/pr-N branch (which lets e2e run as a `push` with the test-gateway
# secrets). Called by .github/workflows/fork-e2e-mirror.yml.
#
# Gate (any one opens it):
#   1. The fork-e2e/pr-N branch already exists -> always re-mirror, so new
#      commits on an already-opened PR re-run e2e.
#   2. Returning contributor -- author_association is OWNER / MEMBER /
#      COLLABORATOR / CONTRIBUTOR (GitHub's own "has contributed before"
#      signal; first-timers are FIRST_TIME_CONTRIBUTOR / FIRST_TIMER / NONE).
#   3. Maintainer-approved -- the author is in .github/MAINTAINER, or a
#      maintainer's latest non-COMMENTED review is APPROVED.
#
# Case 3 deliberately mirrors maintainer-approval.yml's computation (same
# MAINTAINER list via load-maintainers.sh, same review semantics). Keep the two
# in sync; a drift only over-/under-opens the mirror gate (bounded by the
# rate-limited, revocable test token), it can't bypass the merge gate.
#
# Env in:  GH_TOKEN, REPO, PR, AUTHOR_ASSOCIATION, MAINTAINERS (space-separated,
#          from load-maintainers.sh), MIRROR_BRANCH (e.g. fork-e2e/pr-123).
# Out:     `mirror=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

emit() {
  echo "mirror=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "mirror=$1 ($2)"
}

# 1. Already opened: re-mirror every subsequent push.
if gh api "repos/$REPO/branches/$MIRROR_BRANCH" >/dev/null 2>&1; then
  emit true "re-mirror: $MIRROR_BRANCH already exists"
  exit 0
fi

# 2. Returning contributor (GitHub's native author_association signal).
case "$AUTHOR_ASSOCIATION" in
  OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR)
    emit true "returning contributor (author_association=$AUTHOR_ASSOCIATION)"
    exit 0
    ;;
esac

# 3. Maintainer-approved (mirrors maintainer-approval.yml; see header note).
MAINTAINERS_LC=$(echo "${MAINTAINERS:-}" | tr '[:upper:]' '[:lower:]')
if [[ -n "${MAINTAINERS_LC// /}" ]]; then
  AUTHOR=$(gh pr view "$PR" --repo "$REPO" --json author --jq '.author.login')
  AUTHOR_LC=$(echo "$AUTHOR" | tr '[:upper:]' '[:lower:]')

  for m in $MAINTAINERS_LC; do
    if [[ "$m" == "$AUTHOR_LC" ]]; then
      emit true "author @$AUTHOR is a maintainer"
      exit 0
    fi
  done

  # Latest non-COMMENTED review per reviewer; APPROVED by a maintainer opens it.
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
fi

emit false "awaiting maintainer approval (first-time contributor)"
