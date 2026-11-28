// Check Again for a comment change GitHub didn't confirm: read the comment through the session's
// host again, and settle the change from what GitHub has now. Shared by the card that shows the
// comment and by the panel's list of changes whose comment isn't shown.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import type { QueryClient } from "@tanstack/react-query";
import { invalidatePendingReview } from "@/pullRequestReviews/usePendingReview";
import {
  type CommentEditEntry,
  EMPTY_ENTRY,
  checkFailed,
  checkStarted,
  nextCommentCheck,
  readCommentEdit,
  settleChange,
  updateCommentEdit,
} from "./commentEditStore";
import { readCommentThroughHost } from "./hostCommentCheck";

/**
 * Read the held change's comment and settle it. Only the latest check's answer counts; a change
 * that isn't held, or wasn't sent through the host, is left as it is.
 */
export function checkCommentChange(
  queryClient: QueryClient,
  conversationId: string,
  key: string,
): void {
  const unknown = readCommentEdit(key).unknown;
  if (!unknown?.check.host) return;
  const check = nextCommentCheck();
  updateCommentEdit(key, (stored) => checkStarted(stored, check));
  void readCommentThroughHost(queryClient, conversationId, unknown.check)
    .then((found) => {
      if (!found) {
        updateCommentEdit(key, (stored) => checkFailed(stored, check));
        return;
      }
      settle(queryClient, conversationId, key, check, found);
    })
    .catch(() => updateCommentEdit(key, (stored) => checkFailed(stored, check)));
}

function settle(
  queryClient: QueryClient,
  conversationId: string,
  key: string,
  check: number,
  found: { state: "present"; body: string } | { state: "absent" },
): void {
  const target = readCommentEdit(key).unknown?.check;
  const outcome = settleChange(readCommentEdit(key), check, found);
  if (!outcome || !target) return;
  if (outcome.settled === "edited" || outcome.settled === "deleted") {
    updateCommentEdit(key, () => EMPTY_ENTRY);
    void invalidateSessionPullRequest(queryClient, conversationId, target.prUrl, [
      "pr-page-github-info",
    ]);
    void invalidateSessionPullRequest(queryClient, conversationId, target.prUrl, [
      "pr-page-github-review-threads",
    ]);
    // A draft shows from the pending review's read, which says what it is now.
    if (target.kind === "review") invalidatePendingReview(queryClient, target.prUrl);
    return;
  }
  updateCommentEdit(key, () => outcome.entry);
}

/** Whether Check Again can read the held change's comment now. */
export function commentCheckReady(entry: CommentEditEntry): boolean {
  return entry.unknown !== null;
}
