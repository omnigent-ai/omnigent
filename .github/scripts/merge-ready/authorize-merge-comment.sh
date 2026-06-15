#!/usr/bin/env bash
# Authorizes a `/merge` slash command by the commenter's repo access.
#
# `/merge` only enables auto-merge / direct-merges an already-mergeable
# PR -- branch protection still blocks red or unreviewed PRs -- so the
# bar is repo write access, not the stricter MAINTAINER set that gates
# `force-merge`. This keeps `/merge` usable by the whole team while
# blocking outside contributors and drive-by accounts.
#
# The job-level `if` already pre-filters on author_association as a
# cheap first pass; this is the authoritative check, because an org
# MEMBER does not necessarily have write on this specific repo. The
# permission API resolves effective access (team grants, etc.).
#
# Env in: GH_TOKEN, REPO, AUTHOR, PR
# Out:    authorized=true|false on $GITHUB_OUTPUT. On false, posts a
#         reply comment explaining the rejection.

set -euo pipefail

# Effective permission for the commenter: admin|maintain|write|triage|read|none
set +e
PERM=$(gh api "repos/$REPO/collaborators/$AUTHOR/permission" --jq '.permission' 2>/dev/null)
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  # 403/404 => not a collaborator with resolvable permission.
  PERM="none"
fi

case "$PERM" in
  admin|maintain|write)
    echo "authorized=true" >> "$GITHUB_OUTPUT"
    echo "Authorized: @$AUTHOR has '$PERM' access."
    ;;
  *)
    echo "authorized=false" >> "$GITHUB_OUTPUT"
    echo "::notice::@$AUTHOR has '$PERM' access; /merge requires write."
    gh pr comment "$PR" --repo "$REPO" \
      --body ":no_entry: \`/merge\` from @$AUTHOR ignored -- it requires write access to this repository."
    ;;
esac
