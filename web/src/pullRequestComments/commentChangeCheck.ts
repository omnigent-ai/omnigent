// Check Again for a comment change GitHub didn't confirm: read the comment as the Mac account
// that sent the change, and settle the change from what GitHub has now. Shared by the card
// that shows the comment and by the panel's list of changes whose comment isn't shown.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import type { QueryClient } from "@tanstack/react-query";
import { invalidatePendingReview } from "@/pullRequestReviews/usePendingReview";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  macRouteAs,
  macRouteCurrent,
  parseCommentStateAnswer,
} from "@/lib/desktopPullRequestWrites";
import { readCommentThroughHost } from "./hostCommentCheck";
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

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/**
 * Who Check Again must read as when this Mac reads as someone else now, or null when it can
 * check (or nothing is held).
 */
export function checkNeedsLogin(entry: CommentEditEntry): string | null {
  const unknown = entry.unknown;
  if (!unknown || unknown.check.host) return null;
  return macRouteAs(unknown.check.prUrl, unknown.check) ? null : unknown.check.login;
}

/** Whether Check Again can read the held change's comment now. */
export function commentCheckReady(entry: CommentEditEntry): boolean {
  return entry.unknown !== null && checkNeedsLogin(entry) === null;
}

/**
 * Read the held change's comment and settle it. Only the latest check's answer counts, and
 * only an answer read as the account that made the change, still signed in as when the check
 * started; anything else leaves it held.
 */
export function checkCommentChange(
  queryClient: QueryClient,
  conversationId: string,
  key: string,
): void {
  const entry = readCommentEdit(key);
  const unknown = entry.unknown;
  if (unknown?.check.host) {
    checkThroughHost(queryClient, conversationId, key);
    return;
  }
  // As the same login and scope the change was sent as, under its sign-in now.
  const route = unknown ? macRouteAs(unknown.check.prUrl, unknown.check) : null;
  if (!unknown || !route || !commentCheckReady(entry)) return;
  const check = nextCommentCheck();
  const target = unknown.check;
  updateCommentEdit(key, (stored) => checkStarted(stored, check));
  void readOnThisMac<unknown>(route.target, "pull_request_comment_state", {
    repository: target.repository,
    number: target.number,
    comment_id: target.commentId,
    kind: target.kind,
  })
    .then(({ data, viewer: login }) => {
      const found = parseCommentStateAnswer(data, target.commentId);
      // A sign-in again, or another account, during the check: its answer isn't the sender's.
      const stillActing = macRouteCurrent(target.prUrl, route) !== null;
      if (
        !found ||
        found.state === "unreadable" ||
        !login ||
        !sameLogin(login, target.login) ||
        !stillActing
      ) {
        updateCommentEdit(key, (stored) => checkFailed(stored, check));
        return;
      }
      settle(queryClient, conversationId, key, check, found);
    })
    .catch(() => updateCommentEdit(key, (stored) => checkFailed(stored, check)));
}

/** Check a change the session's host made, from the PR's reads through the session again. */
function checkThroughHost(queryClient: QueryClient, conversationId: string, key: string): void {
  const unknown = readCommentEdit(key).unknown;
  if (!unknown) return;
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
    void invalidateSessionPullRequest(queryClient, conversationId, target.prUrl, ["github-info"]);
    void invalidateSessionPullRequest(queryClient, conversationId, target.prUrl, [
      "github-review-threads",
    ]);
    // A draft shows from the pending review's read, which says what it is now.
    if (target.kind === "review") invalidatePendingReview(queryClient, target.prUrl);
    return;
  }
  updateCommentEdit(key, () => outcome.entry);
}
