// The session's host as the route for a pending review, where this shell has no route of its own.
// It reads and writes as the host's account, through the route the session's PR page was decided
// on; a change captured on one route is never sent along another.

import { type CodeHostPullRequest, parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import type { Method } from "@/lib/workspaceApi";
import { hostChangeRoute } from "@/pullRequestPage/pullRequestPageRoute";

/** What each pending-review step is through the host; the server gates each for the caller. */
export const HOST_PENDING_METHODS = {
  read: "getPendingReview",
  start: "startPendingReview",
  add: "addPendingReviewComment",
  edit: "updatePendingReviewComment",
  delete: "deletePendingReviewComment",
  submit: "submitPendingReview",
  discard: "discardPendingReview",
} as const satisfies Record<string, Method>;

export type HostPendingStep = keyof typeof HOST_PENDING_METHODS;

/** A session's host as the route a pending review is read and changed along. */
export interface HostPendingRoute {
  kind: "host";
  conversationId: string;
  /** The route key the session's page was decided on; another key is another route. */
  key: string;
  prUrl: string;
  target: CodeHostPullRequest;
  /** The host reads as its own account, which it doesn't name to the page. */
  actor: { login: string; generation: 0 };
  reader: null;
}

/** Where a host route sends: its session and decided route, for scoping what it sent. */
export interface HostPendingScope {
  conversationId: string;
  key: string;
}

/** The host route for `prUrl` while the session's route serves the pending review read. */
export function hostPendingRoute(conversationId: string, prUrl: string): HostPendingRoute | null {
  if (!conversationId) return null;
  const change = hostChangeRoute(conversationId, HOST_PENDING_METHODS.read);
  const target = change ? parseCodeHostPullRequestUrl(prUrl) : null;
  if (!change || target?.provider !== "github") return null;
  return {
    kind: "host",
    conversationId,
    key: change.key,
    prUrl,
    target,
    actor: { login: "", generation: 0 },
    reader: null,
  };
}

/** Whether `scope`'s session still reads `prUrl`'s pending review along the same route. */
export function hostPendingScopeCurrent(scope: HostPendingScope, prUrl: string): boolean {
  return hostPendingRoute(scope.conversationId, prUrl)?.key === scope.key;
}

/** Whether `route` is still the route, and its route serves `step` for this caller. */
export function hostPendingOffers(route: HostPendingRoute, step: HostPendingStep): boolean {
  if (!hostPendingScopeCurrent(route, route.prUrl)) return false;
  return hostChangeRoute(route.conversationId, HOST_PENDING_METHODS[step])?.key === route.key;
}

export const hostPendingScopeOf = (route: HostPendingRoute): HostPendingScope => ({
  conversationId: route.conversationId,
  key: route.key,
});

/**
 * Who a pending review's changes are, through a session's host: that session and route, as
 * the login a Mac route's changes are kept under. Another session's host is another identity.
 */
export const hostPendingIdentity = (scope: HostPendingScope): string =>
  `host:${scope.conversationId}@${scope.key}`;

export const isHostPendingRoute = (route: object): route is HostPendingRoute =>
  (route as { kind?: unknown }).kind === "host";
