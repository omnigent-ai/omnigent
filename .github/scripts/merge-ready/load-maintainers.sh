#!/usr/bin/env bash
# Loads the maintainer set from .github/MAINTAINER at main's tip.
#
# Always main, never the PR head SHA: otherwise a PR could edit
# MAINTAINER to grant itself a maintainer-gated waiver (e.g.
# skip-security-scan, skip-e2e-ui-test) without being merged.
# Defense-in-depth: a PR could still edit *this* workflow to drop
# `?ref=main`, so the remaining defense is `required_pull_request_reviews`
# in branch protection.
#
# MAINTAINER format: one bare username per line, with comments (`#`)
# and blank lines ignored.
#
# Env in: GH_TOKEN, REPO
# Out:    `list=<space-separated usernames>` on $GITHUB_OUTPUT (empty
#         when MAINTAINER is missing or empty).

set -euo pipefail

set +e
CONTENT_B64=$(gh api "repos/$REPO/contents/.github/MAINTAINER?ref=main" --jq '.content' 2>/dev/null)
RC=$?
set -e

if [[ $RC -ne 0 || -z "$CONTENT_B64" ]]; then
  echo "list=" >> "$GITHUB_OUTPUT"
  echo "::warning::.github/MAINTAINER not found on main; maintainer-gated waivers cannot be effective until the file is merged."
  exit 0
fi

CONTENT=$(echo "$CONTENT_B64" | base64 -d)

# `grep -v` exits 1 on no matches; wrap so the pipeline stays 0 under
# pipefail and we reach the empty-list branch.
USERS=$(echo "$CONTENT" | sed -E 's/#.*$//' | tr -s '[:space:]' '\n' | { grep -v '^$' || true; } | tr '\n' ' ')
USERS="${USERS% }"

if [[ -z "${USERS// /}" ]]; then
  echo "list=" >> "$GITHUB_OUTPUT"
  echo "::warning::.github/MAINTAINER on main has no entries; maintainer-gated waivers cannot be effective."
  exit 0
fi

echo "list=$USERS" >> "$GITHUB_OUTPUT"
echo "Loaded maintainers from MAINTAINER@main: $USERS"
