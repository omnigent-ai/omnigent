#!/usr/bin/env bash
# Decides whether a PR's diff should be put through the Security Scan.
# Called by .github/workflows/security-gate.yml.
#
# We scan UNTRUSTED authors and skip trusted ones. "Trusted" is GitHub's
# native author_association: OWNER / MEMBER / COLLABORATOR -- people with a
# direct relationship to the repo/org -- OR an author in the MAINTAINERS list.
# The list covers maintainers whose org membership is PRIVATE: GitHub only
# reports MEMBER in author_association when membership is public, so a private
# maintainer shows up as CONTRIBUTOR and would otherwise be scanned. Everyone
# else is scanned, INCLUDING returning CONTRIBUTORs (a merged PR in the past
# does not vouch for the contents of this one) and first-timers
# (FIRST_TIME_CONTRIBUTOR / NONE).
#
# This gate decides whether to inspect a PR for attacks and errs toward scanning
# more (it scans returning CONTRIBUTORs, not just first-timers).
#
# author_association is computed by GitHub from the actor's relationship to the
# repo at event time; it is not attacker-settable from PR contents.
#
# An untrusted PR needs both `skip-security-scan` and a maintainer's latest
# decisive review to be APPROVED. Triage permission alone cannot waive scanning.
#
# The label is read from the API (trusted), and this script always runs from
# `main`, so a PR cannot edit the decision. The waiver is only evaluated when the
# lookup vars (GH_TOKEN/REPO/PR) are passed (the scan does; the per-workflow
# pollers do not -- they just mirror the scan's result).
#
# Env in:  EVENT_NAME          (github.event_name)
#          AUTHOR_ASSOCIATION  (github.event.pull_request.author_association)
#          PR_AUTHOR           (github.event.pull_request.user.login; trusted-CI-bot
#                               allowlist, checked without an API call so the gate
#                               pollers short-circuit too)
#          MAINTAINERS         (space-separated, from merge-ready/load-maintainers.sh;
#                               used for maintainer authors and review waivers)
#          GH_TOKEN, REPO, PR  (for the label lookup + author check)
# Out:     `scan=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

SKIP_LABEL="skip-security-scan"

emit() {
  echo "scan=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "scan=$1 ($2)"
}

# Missing credentials or a failed label lookup cannot waive scanning.
has_skip_label() {
  [[ -n "${GH_TOKEN:-}" && -n "${REPO:-}" && -n "${PR:-}" ]] || return 1

  local has_label
  has_label=$(gh api "repos/$REPO/pulls/$PR" \
    --jq "[.labels[].name] | index(\"$SKIP_LABEL\") != null" 2>/dev/null || echo "false")
  [[ "$has_label" == "true" ]]
}

# Fetch every review page before selecting each maintainer's latest decisive
# review. Comments leave approvals intact; dismissals and changes requested revoke them.
has_maintainer_waiver() {
  [[ -n "${MAINTAINERS:-}" && -n "${MAINTAINERS// /}" ]] || return 1
  has_skip_label || return 1

  local reviews approvers maint_lc reviewer maintainer
  reviews=$(gh api "repos/$REPO/pulls/$PR/reviews?per_page=100" \
    --paginate --slurp 2>/dev/null) || return 1
  approvers=$(jq -r \
    '[.[][] | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
      | {login: ((.user.login // "") | ascii_downcase), state, submitted_at, id}]
      | group_by(.login)[] | max_by([.submitted_at, .id])
      | select(.state == "APPROVED") | .login' <<<"$reviews" 2>/dev/null) || return 1
  maint_lc=$(echo "$MAINTAINERS" | tr '[:upper:]' '[:lower:]')
  for reviewer in $approvers; do
    for maintainer in $maint_lc; do
      [[ "$reviewer" == "$maintainer" ]] && return 0
    done
  done
  return 1
}

# Only PRs carry untrusted contributor code through the gate. Every other
# trigger -- push to main, schedule, dispatch -- is a trusted context, so
# proceed without scanning. Reviews carry the same pull_request fields and
# re-evaluate the waiver when an approval is submitted or dismissed.
case "${EVENT_NAME:-}" in
  pull_request | pull_request_target | pull_request_review) ;;
  *)
    emit false "non-PR event (${EVENT_NAME:-unknown}); trusted context"
    exit 0
    ;;
esac

# Author is a known maintainer? `author_association` only reports MEMBER when
# the org membership is PUBLIC, so a maintainer with private membership shows up
# as CONTRIBUTOR in the event payload and would otherwise be scanned. The
# MAINTAINERS list (from load-maintainers.sh) is authoritative and trusted, so
# trust the author directly when they appear in it. Only evaluated when
# MAINTAINERS is passed (the scan does; the per-workflow pollers do not).
author_is_maintainer() {
  [[ -n "${MAINTAINERS:-}" && -n "${MAINTAINERS// /}" ]] || return 1
  [[ -n "${GH_TOKEN:-}" && -n "${REPO:-}" && -n "${PR:-}" ]] || return 1

  local maint_lc author_lc
  maint_lc=$(echo "$MAINTAINERS" | tr '[:upper:]' '[:lower:]')
  author_lc=$(gh pr view "$PR" --repo "$REPO" --json author --jq '.author.login' 2>/dev/null \
    | tr '[:upper:]' '[:lower:]')
  [[ -n "$author_lc" ]] || return 1
  for m in $maint_lc; do
    [[ "$m" == "$author_lc" ]] && return 0
  done
  return 1
}

# Trusted CI bots (omni-resolve-agent, omnigent-ci) open SAME-REPO PRs from a
# trusted internal pipeline via their GitHub App, and every such PR is still
# gated by Maintainer Approval + human review before merge. The PR author login
# is set by GitHub and is not settable from PR contents, and a fork PR's author
# is never one of these bots. Checked from PR_AUTHOR with NO API call, so this
# short-circuits the per-workflow gate pollers too (they pass no token) -- not
# just the scan -- sparing the shared GITHUB_TOKEN budget their high PR volume
# was exhausting. Add a login here only for a bot whose PRs are trusted to skip
# the diff scan.
TRUSTED_BOTS="omni-resolve-agent[bot] omnigent-ci[bot]"
if [[ -n "${PR_AUTHOR:-}" ]]; then
  for bot in $TRUSTED_BOTS; do
    [[ "$PR_AUTHOR" == "$bot" ]] && { emit false "trusted CI bot ($PR_AUTHOR)"; exit 0; }
  done
fi

case "${AUTHOR_ASSOCIATION:-}" in
  OWNER | MEMBER | COLLABORATOR)
    emit false "trusted author (author_association=$AUTHOR_ASSOCIATION)"
    ;;
  *)
    if author_is_maintainer; then
      emit false "trusted author (maintainer; author_association=${AUTHOR_ASSOCIATION:-unknown})"
    elif has_maintainer_waiver; then
      emit false "'$SKIP_LABEL' waiver (approved by a maintainer)"
    else
      emit true "untrusted author (author_association=${AUTHOR_ASSOCIATION:-unknown})"
    fi
    ;;
esac
