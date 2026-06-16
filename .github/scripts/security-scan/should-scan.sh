#!/usr/bin/env bash
# Decides whether a PR's diff should be put through the Security Scan.
# Called by .github/workflows/security-scan.yml.
#
# We scan UNTRUSTED authors and skip trusted ones. "Trusted" is GitHub's
# native author_association: OWNER / MEMBER / COLLABORATOR -- people with a
# direct relationship to the repo/org. Everyone else is scanned, INCLUDING
# returning CONTRIBUTORs (a merged PR in the past does not vouch for the
# contents of this one) and first-timers (FIRST_TIME_CONTRIBUTOR / NONE).
#
# This is deliberately stricter than fork-e2e/should-mirror.sh, which trusts
# CONTRIBUTOR: that gate only decides whether to spend a rate-limited test
# token, whereas this gate decides whether to inspect for attacks, so it errs
# toward scanning more.
#
# author_association is computed by GitHub from the actor's relationship to the
# repo at event time; it is not attacker-settable from PR contents.
#
# Env in:  EVENT_NAME          (github.event_name)
#          AUTHOR_ASSOCIATION  (github.event.pull_request.author_association)
# Out:     `scan=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

emit() {
  echo "scan=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "scan=$1 ($2)"
}

# Only PRs carry untrusted contributor code through the gate. Every other
# trigger -- push to main / fork-e2e/** (the mirror branch only exists after a
# returning-contributor / maintainer-approval gate), schedule, dispatch -- is a
# trusted context, so proceed without scanning.
case "${EVENT_NAME:-}" in
  pull_request | pull_request_target) ;;
  *)
    emit false "non-PR event (${EVENT_NAME:-unknown}); trusted context"
    exit 0
    ;;
esac

case "${AUTHOR_ASSOCIATION:-}" in
  OWNER | MEMBER | COLLABORATOR)
    emit false "trusted author (author_association=$AUTHOR_ASSOCIATION)"
    ;;
  *)
    emit true "untrusted author (author_association=${AUTHOR_ASSOCIATION:-unknown})"
    ;;
esac
