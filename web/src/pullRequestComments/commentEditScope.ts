// Whose a comment change's text is: a published comment's edit is the workspace user's, while a
// private draft's is only the Mac account and scope that read it, until that scope is back.

import { macPendingReviewOffers, macWriteRoute } from "@/lib/desktopPullRequestWrites";
import { type MacNamedReader, sameNamedReader } from "@/lib/macNamedReads";
import {
  type HostPendingScope,
  hostPendingScopeCurrent,
} from "@/pullRequestReviews/hostPendingRoute";

import type { CommentEditEntry } from "./commentEditStore";

/**
 * Whether the page writes `prUrl` as `scope` now, for the view reading as `readAs`: the same
 * login and scope (the host's account, or the same named login), with pending reviews offered.
 * Anything private to a scope shows, and can be acted on, only while this holds.
 */
export function pendingScopeCurrent(
  scope: { login: string; reader: MacNamedReader | null; host?: HostPendingScope },
  prUrl: string | null,
  readAs: MacNamedReader | null,
): boolean {
  // Sent through the session's host: current while the page reads along the same route.
  if (scope.host) return !!prUrl && hostPendingScopeCurrent(scope.host, prUrl);
  const route = prUrl ? macWriteRoute(prUrl, readAs) : null;
  return (
    !!route &&
    macPendingReviewOffers(route) &&
    route.actor.login.toLowerCase() === scope.login.toLowerCase() &&
    sameNamedReader(route.reader, scope.reader)
  );
}

/**
 * Whether `entry` may show on `prUrl` for the view reading as `readAs`: a published comment's
 * always, a private draft's only while its scope is current. Another scope's text stays kept.
 */
export function commentEditVisible(
  entry: CommentEditEntry,
  prUrl: string | null,
  readAs: MacNamedReader | null,
): boolean {
  return !entry.privateScope || pendingScopeCurrent(entry.privateScope, prUrl, readAs);
}
