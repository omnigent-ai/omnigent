#!/usr/bin/env bash
# Fulfills durable automerge intent after the gate publishes its current status.
# Direct-merge fallback requires the same head and fresh green CI.
#
# Env in: GH_TOKEN, REPO, PR, SHA, GATE_STATE, EXPIRES_AT

set -euo pipefail

request_is_current() {
  local pull
  pull=$(gh api "repos/$REPO/pulls/$PR") || return 1
  [[ $(jq -r '.state' <<<"$pull") == 'open' && $(jq -r '.head.sha' <<<"$pull") == "$SHA" ]] \
    && jq -e '.labels | any(.name == "automerge")' <<<"$pull" >/dev/null
}

if ! request_is_current; then
  echo "::notice::Skipped auto-merge: PR closed, head changed, or automerge label removed."
  exit 0
fi

gate_is_fresh() {
  local now
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  [[ "$GATE_STATE" == 'success' && -n "$EXPIRES_AT" && "$EXPIRES_AT" > "$now" ]]
}

# `gh pr merge --auto` can merge immediately when the status is already green.
if [[ "$GATE_STATE" == 'success' ]] && ! gate_is_fresh; then
  echo "::notice::CI expired before auto-merge; the request remains queued for fresh CI."
  exit 0
fi

set +e
MERGE_OUT=$(gh pr merge "$PR" --repo "$REPO" --squash --auto --delete-branch --match-head-commit "$SHA" 2>&1)
MERGE_RC=$?
MODE="Auto-merge enabled"
if [[ $MERGE_RC -ne 0 ]] \
   && echo "$MERGE_OUT" | grep -qE "clean status|Base branch was modified" \
   && [[ "$GATE_STATE" == 'success' ]] \
   && request_is_current \
   && gate_is_fresh; then
  MERGE_OUT=$(gh pr merge "$PR" --repo "$REPO" --squash --delete-branch --match-head-commit "$SHA" 2>&1)
  MERGE_RC=$?
  MODE="Merged directly"
fi
set -e
echo "$MERGE_OUT"

if [[ $MERGE_RC -eq 0 ]]; then
  echo "::notice::$MODE via 'automerge' label."
else
  echo "::warning::Could not enable auto-merge via 'automerge' label: $MERGE_OUT"
fi
