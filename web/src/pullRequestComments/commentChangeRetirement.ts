// A comment edit or delete on its way to GitHub was sent through the session's host on the route
// its page was decided on. If that route changes before GitHub answers, the answer is ignored and
// the change retires into the held state Check Again settles: its text stays, and nothing more is
// sent for that comment.

import type { CommentChangeOperation } from "@/lib/pullRequestStateHolds";
import type { Method } from "@/lib/workspaceApi";
import { hostChangeRouteCurrent } from "@/pullRequestPage/pullRequestPageRoute";
import { type CommentCheckTarget, changeUncertain, updateCommentEdit } from "./commentEditStore";

/** What a sent change captured at the click. */
export interface PendingCommentChange {
  /** The comment's entry in the edit store, as of the click. */
  key: string;
  requestId: string;
  prUrl: string;
  operation: CommentChangeOperation;
  /** What the held state keeps, for Check Again. */
  held: { sent?: string; expected?: string; check: CommentCheckTarget };
  /** The method the change went by, which its route must still serve. */
  hostMethod: Method;
}

const pending = new Map<string, PendingCommentChange>();

/** Whether the change could still be sent as it was: same route and method. */
function stillAsSent(change: PendingCommentChange): boolean {
  const { host } = change.held.check;
  return !!host && hostChangeRouteCurrent(host, change.hostMethod);
}

/**
 * Track `change` until GitHub answers. Returns `settle`, to call with the answer: true when the
 * change was still current, so the answer may be applied; false when it had retired.
 */
export function watchCommentChange(change: PendingCommentChange): () => boolean {
  pending.set(change.requestId, change);
  return () => {
    const current = pending.has(change.requestId) && stillAsSent(change);
    if (pending.delete(change.requestId) && !current) {
      updateCommentEdit(change.key, (entry) =>
        changeUncertain(entry, change.requestId, change.held),
      );
    }
    return current;
  };
}

/** Whether the change sent as `requestId` hasn't settled yet. */
export function commentChangeCurrent(requestId: string): boolean {
  return pending.has(requestId);
}

/** Forget every watched change, so a test starts clean. */
export function resetCommentChangeRetirementForTests(): void {
  pending.clear();
}
