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
# Maintainer escape hatch: an untrusted PR can be waived by the
# `skip-security-scan` label alone. Applying a label requires GitHub Triage
# permission (or higher), which a fork author never has, so the label IS the
# maintainer gate and no separate approval is required.
#
# ACCEPTED RISK (repo policy, not GitHub-enforced): GitHub allows the Triage role
# to be granted independently of Write, so in principle a triage-only collaborator
# could self-waive. We accept this because this repo grants Triage only to
# write/admin collaborators -- everyone who can apply the label can already push
# code, so the waiver grants no privilege they don't already have. This invariant
# lives in repo settings, not in code; if Triage is ever granted without Write,
# revisit (e.g. re-add a maintainer-list check). See the PR for the full rationale.
#
# The label is read from the API (trusted), and this script always runs from
# `main`, so a PR cannot edit the decision. The waiver is only evaluated when the
# lookup vars (GH_TOKEN/REPO/PR) are passed (the scan does; the per-workflow
# pollers do not -- they just mirror the scan's result).
#
# Env in:  EVENT_NAME          (github.event_name)
#          AUTHOR_ASSOCIATION  (github.event.pull_request.author_association)
#          MAINTAINERS         (space-separated, from merge-ready/load-maintainers.sh;
#                               optional -- used only to trust private-membership
#                               maintainer AUTHORS, not for the label waiver)
#          GH_TOKEN, REPO, PR  (for the label lookup + author check)
# Out:     `scan=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

SKIP_LABEL="skip-security-scan"

emit() {
  echo "scan=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "scan=$1 ($2)"
}

# 0 = the skip label is present; 1 otherwise. Label-only: applying the label
# already requires Triage permission (or higher), so its mere presence is the
# maintainer gate (see the accepted-risk note in the header). Fails closed on any
# gap (missing token, etc).
has_skip_label() {
  [[ -n "${GH_TOKEN:-}" && -n "${REPO:-}" && -n "${PR:-}" ]] || return 1

  local has_label
  has_label=$(gh api "repos/$REPO/pulls/$PR" \
    --jq "[.labels[].name] | index(\"$SKIP_LABEL\") != null" 2>/dev/null || echo "false")
  [[ "$has_label" == "true" ]]
}

# Only PRs carry untrusted contributor code through the gate. Every other
# trigger -- push to main, schedule, dispatch -- is a trusted context, so
# proceed without scanning. pull_request_review is still
# accepted (it carries the same pull_request + author_association fields, so the
# gate evaluates identically) in case a workflow_call caller is wired to it, but
# no workflow triggers a scan on review any more: the skip-security-scan waiver
# is label-only, so the label event alone re-runs the scan and flips the check.
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

case "${AUTHOR_ASSOCIATION:-}" in
  OWNER | MEMBER | COLLABORATOR)
    emit false "trusted author (author_association=$AUTHOR_ASSOCIATION)"
    ;;
  *)
    if author_is_maintainer; then
      emit false "trusted author (maintainer; author_association=${AUTHOR_ASSOCIATION:-unknown})"
    elif has_skip_label; then
      emit false "'$SKIP_LABEL' waiver (label requires a Triage+ collaborator to apply)"
    else
      emit true "untrusted author (author_association=${AUTHOR_ASSOCIATION:-unknown})"
    fi
    ;;
esac
