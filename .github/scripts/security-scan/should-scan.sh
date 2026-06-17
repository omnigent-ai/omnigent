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
# This gate is independent of fork-e2e/should-mirror.sh: that one gates secret-
# bearing e2e on the maintainer-applied `e2e-approved` label, whereas this gate
# decides whether to inspect for attacks and so errs toward scanning more (it
# scans returning CONTRIBUTORs that the label gate would not by itself run).
#
# author_association is computed by GitHub from the actor's relationship to the
# repo at event time; it is not attacker-settable from PR contents.
#
# Maintainer escape hatch: an untrusted PR can be waived by the
# `skip-security-scan` label, but ONLY when the waiver is maintainer-effective
# -- the label is present AND the author is a maintainer, or a maintainer's
# latest decisive review is APPROVED. Same semantics as e2e-ui-required's
# `skip-e2e-ui-test` (and force-merge): the label alone is not enough, so a fork
# author cannot self-waive (applying labels needs triage access anyway, and the
# extra maintainer check is defence in depth). All state is read from the API
# (trusted), and this script always runs from `main`, so a PR cannot edit the
# decision. The waiver is only evaluated when MAINTAINERS is passed (the scan
# does; the per-workflow pollers do not -- they just mirror the scan's result).
#
# Env in:  EVENT_NAME          (github.event_name)
#          AUTHOR_ASSOCIATION  (github.event.pull_request.author_association)
#          MAINTAINERS         (space-separated, from merge-ready/load-maintainers.sh;
#                               optional -- when empty the skip label is ignored)
#          GH_TOKEN, REPO, PR  (for the waiver lookup; needed only with MAINTAINERS)
# Out:     `scan=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

SKIP_LABEL="skip-security-scan"

emit() {
  echo "scan=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "scan=$1 ($2)"
}

# 0 = the skip label is present AND backed by a maintainer; 1 otherwise.
# Mirrors e2e-ui-required/check.sh cases 3-4. Fails closed on any gap.
skip_label_effective() {
  [[ -n "${GH_TOKEN:-}" && -n "${REPO:-}" && -n "${PR:-}" ]] || return 1
  [[ -n "${MAINTAINERS:-}" && -n "${MAINTAINERS// /}" ]] || return 1

  local has_label
  has_label=$(gh api "repos/$REPO/pulls/$PR" \
    --jq "[.labels[].name] | index(\"$SKIP_LABEL\") != null" 2>/dev/null || echo "false")
  [[ "$has_label" == "true" ]] || return 1

  local maint_lc author_lc approvers u_lc
  maint_lc=$(echo "$MAINTAINERS" | tr '[:upper:]' '[:lower:]')

  # Author is a maintainer?
  author_lc=$(gh pr view "$PR" --repo "$REPO" --json author --jq '.author.login' 2>/dev/null \
    | tr '[:upper:]' '[:lower:]')
  for m in $maint_lc; do
    [[ "$m" == "$author_lc" ]] && return 0
  done

  # A maintainer's latest decisive (non-COMMENTED) review is APPROVED?
  approvers=$(gh api "repos/$REPO/pulls/$PR/reviews" --paginate \
    --jq '[.[] | select(.state != "COMMENTED")] | group_by(.user.login) | map(max_by(.submitted_at)) | .[] | select(.state == "APPROVED") | .user.login' 2>/dev/null || echo "")
  for u in $approvers; do
    u_lc=$(echo "$u" | tr '[:upper:]' '[:lower:]')
    for m in $maint_lc; do
      [[ "$m" == "$u_lc" ]] && return 0
    done
  done

  return 1
}

# Only PRs carry untrusted contributor code through the gate. Every other
# trigger -- push to main / fork-e2e/** (the mirror branch only exists after a
# returning-contributor / maintainer-approval gate), schedule, dispatch -- is a
# trusted context, so proceed without scanning. pull_request_review is included
# because the fork-e2e mirror fires on a maintainer's approval, and that path
# must still consult the head SHA's Security Scan (the review payload carries
# the same pull_request + author_association fields).
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
    elif skip_label_effective; then
      emit false "maintainer-effective '$SKIP_LABEL' waiver"
    else
      emit true "untrusted author (author_association=${AUTHOR_ASSOCIATION:-unknown})"
    fi
    ;;
esac
