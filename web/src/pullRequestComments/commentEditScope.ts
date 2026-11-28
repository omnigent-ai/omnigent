// Whose a comment change's text is: a published comment's edit is the workspace user's, while a
// private draft's is only the session's host account and route that read it, until that route
// is back.

import {
  type HostPendingScope,
  hostPendingScopeCurrent,
} from "@/pullRequestReviews/hostPendingRoute";

import type { CommentEditEntry } from "./commentEditStore";

/**
 * Whether the page writes `prUrl` through the session's host now, along the route the draft was
 * read on. Anything private to a scope shows, and can be acted on, only while this holds.
 */
export function pendingScopeCurrent(
  scope: { host?: HostPendingScope },
  prUrl: string | null,
): boolean {
  return !!scope.host && !!prUrl && hostPendingScopeCurrent(scope.host, prUrl);
}

/**
 * Whether `entry` may show on `prUrl` in `sessionId`'s view: a published comment's always, a
 * private draft's only in the session it was read in, while its route is current. Another
 * session's or route's text stays kept for it.
 */
export function commentEditVisible(
  entry: CommentEditEntry,
  prUrl: string | null,
  sessionId: string,
): boolean {
  const scope = entry.privateScope;
  if (!scope) return true;
  return scope.host?.conversationId === sessionId && pendingScopeCurrent(scope, prUrl);
}
