// A comment edit or delete on its way to GitHub was sent as one Mac account, on one route,
// with comment edits on, or through the session's host on the route its page was decided on.
// If any of that changes before GitHub answers, the change retires
// into the held state Check Again settles: its text stays, nothing more is sent for that
// comment, and the late answer is ignored. Watched outside React, so it happens even when no
// card showing the comment is mounted.

import {
  type CommentChangeOperation,
  macCommentChangeOffers,
  macRouteCurrent,
  subscribeMacWriteRoute,
} from "@/lib/desktopPullRequestWrites";
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
  /** The Mac account the change was sent as. */
  actor: { login: string; generation: number };
  /** What the held state keeps, for Check Again. */
  held: { sent?: string; expected?: string; check: CommentCheckTarget };
  /** Through the session's host: the method it went by, which its route must still serve. */
  hostMethod?: Method;
}

const pending = new Map<string, PendingCommentChange>();
let stopWatching: (() => void) | null = null;

/** Whether the change could still be sent as it was: same account, route and flag. */
function stillAsSent(change: PendingCommentChange): boolean {
  const { host } = change.held.check;
  if (host && change.hostMethod) return hostChangeRouteCurrent(host, change.hostMethod);
  const route = macRouteCurrent(change.prUrl, {
    actor: change.actor,
    reader: change.held.check.reader,
  });
  return !!route && macCommentChangeOffers(route, change.operation);
}

function retireChanged(): void {
  for (const change of [...pending.values()]) {
    if (stillAsSent(change)) continue;
    pending.delete(change.requestId);
    updateCommentEdit(change.key, (entry) => changeUncertain(entry, change.requestId, change.held));
  }
  stopWhenIdle();
}

function stopWhenIdle(): void {
  if (pending.size > 0 || !stopWatching) return;
  stopWatching();
  stopWatching = null;
}

/**
 * Watch `change` until GitHub answers. Returns `settle`, to call with the answer: true when
 * the change was still current, so the answer may be applied; false when it had retired.
 */
export function watchCommentChange(change: PendingCommentChange): () => boolean {
  pending.set(change.requestId, change);
  stopWatching ??= subscribeMacWriteRoute(retireChanged);
  return () => {
    // The host's route has no event of its own: an answer after it changed is retired then.
    const current = pending.has(change.requestId) && (!change.hostMethod || stillAsSent(change));
    if (pending.delete(change.requestId) && !current) {
      updateCommentEdit(change.key, (entry) =>
        changeUncertain(entry, change.requestId, change.held),
      );
    }
    stopWhenIdle();
    return current;
  };
}

/** Whether the change sent as `requestId` hasn't retired yet. */
export function commentChangeCurrent(requestId: string): boolean {
  return pending.has(requestId);
}

/** Forget every watched change, so a test starts clean. */
export function resetCommentChangeRetirementForTests(): void {
  stopWatching?.();
  stopWatching = null;
  pending.clear();
}
