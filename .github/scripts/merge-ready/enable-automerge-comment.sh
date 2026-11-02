#!/usr/bin/env bash
# Records an authorized `/merge` request outside the status-update queue.
# Any subsequent gate evaluation can fulfill the durable automerge label.
#
# Env in: GH_TOKEN, REPO, PR, AUTHOR

set -euo pipefail

gh pr edit "$PR" --repo "$REPO" --add-label automerge
gh pr comment "$PR" --repo "$REPO" \
  --body ":robot: \`/merge\` from @$AUTHOR recorded with the \`automerge\` label. Merge Ready will recheck CI and enable squash auto-merge when the branch requirements allow it."
