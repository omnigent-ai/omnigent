// Which PR page a session's pull request tabs show. The new page reads only through the
// workspace API, so it needs a route whose host serves the PR methods; without one, the
// classic page shows, as before.

import { useQuery } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  enabledWorkspaceRoutes,
  servingRoutes,
  workspaceMethodName,
  type Method,
  type Route,
  type RouteAnswers,
} from "@/lib/workspaceApi";

/** The reads every view of the new page makes, so a route serving fewer can't show it. */
export const PULL_REQUEST_PAGE_METHODS: readonly Method[] = [
  "getPullRequest",
  "listPullRequestFiles",
  "getPullRequestDiff",
  "listPullRequestCommits",
  "listReviewThreads",
  "listReviewThreadComments",
];

// Each session's decided route, for changes sent outside React; set as each decision lands.
// A host serves each caller its own methods, so each decision is one user's, by session.
const decidedRoutes = new Map<string, PullRequestPageRoute>();
const decidedFor = (sessionId: string, user: string | null) => `${sessionId}\u0000${user ?? ""}`;

// No route answered, e.g. the host was offline: ask again after this long.
const UNANSWERED_RECHECK_MS = 60_000;

/** The route the new page reads through, and the key its answers are held by. */
export interface PullRequestPageRoute {
  route: Route;
  key: string;
  /** The methods its host serves, by the names GetCapabilities lists. */
  served: ReadonlySet<string>;
}

/** What a session's capability answers decide. */
export interface PullRequestPageDecision {
  route: PullRequestPageRoute | null;
  /** Whether some route answered; a decision none answered is checked again later. */
  answered: boolean;
}

/** The first route, most direct first, whose host serves every method the page reads. */
export function pullRequestPageDecision(
  answers: RouteAnswers,
  user: string | null = null,
): PullRequestPageDecision {
  const needed = PULL_REQUEST_PAGE_METHODS.map(workspaceMethodName);
  const serving = answers.serving.find(({ served }) => needed.every((name) => served.has(name)));
  return {
    // Another user's key is another route, so nothing captured for one is sent for the other.
    route: serving
      ? {
          route: serving.route,
          key: user ? `${serving.key}@${user}` : serving.key,
          served: serving.served,
        }
      : null,
    answered: answers.serving.length > 0,
  };
}

export const pullRequestPageRouteKey = (
  sessionId: string,
  routes: readonly Route[],
  user: string | null = null,
) => ["pull-request-page-route", sessionId, user, ...routes] as const;

/**
 * The route `sessionId`'s new PR page reads through; null shows the classic page, and
 * "loading" shows neither yet, so neither flashes. It holds while the flags give the same
 * routes, so one failed call never swaps the page.
 */
export function usePullRequestPageRoute(
  sessionId: string,
): PullRequestPageRoute | null | "loading" {
  // Read for its re-render: a flag change can add or drop a route.
  useServerInfo();
  // Signing out or in as someone else decides again, for that user's methods.
  const user = useSyncExternalStore(subscribeIdentity, getCurrentUserId);
  const routes = enabledWorkspaceRoutes();
  const decision = useQuery({
    queryKey: pullRequestPageRouteKey(sessionId, routes, user),
    queryFn: async ({ signal }) => {
      const decided = pullRequestPageDecision(await servingRoutes(sessionId, signal), user);
      if (decided.route) decidedRoutes.set(decidedFor(sessionId, user), decided.route);
      else decidedRoutes.delete(decidedFor(sessionId, user));
      return decided;
    },
    // A view with no session, such as one outside any chat, reads through none.
    enabled: routes.length > 0 && sessionId !== "",
    staleTime: (query) => (query.state.data?.answered === false ? UNANSWERED_RECHECK_MS : Infinity),
    refetchInterval: (query) =>
      query.state.data?.answered === false ? UNANSWERED_RECHECK_MS : false,
    refetchOnWindowFocus: false,
    retry: false,
  });
  if (routes.length === 0 || sessionId === "") return null;
  if (decision.data) return decision.data.route;
  return decision.isError ? null : "loading";
}

/** Whether the route `sessionId`'s new page reads through also serves `method`. */
export function usePullRequestMethodServed(sessionId: string, method: Method): boolean {
  return usePullRequestCapabilityServed(sessionId, workspaceMethodName(method));
}

/** {@link usePullRequestMethodServed} by the method's name, for one this client has no binding for yet. */
export function usePullRequestCapabilityServed(sessionId: string, capability: string): boolean {
  const route = usePullRequestPageRoute(sessionId);
  return route !== null && route !== "loading" && route.served.has(capability);
}

/** The session's host, where a change to its PR goes when this Mac has no route for it. */
export interface HostChangeRoute {
  conversationId: string;
  /** The route key its page was decided on; a new one means a new decision. */
  key: string;
}

/**
 * Where `sessionId`'s host makes a change through `method` now: its page's route, while the
 * flags still turn that route on and its host serves the method; else null.
 */
export function hostChangeRoute(sessionId: string, method: Method): HostChangeRoute | null {
  // Only the signed-in user's decision: after a sign-out or switch, nothing goes until theirs.
  const route = decidedRoutes.get(decidedFor(sessionId, getCurrentUserId()));
  if (!route || !enabledWorkspaceRoutes().includes(route.route)) return null;
  if (!route.served.has(workspaceMethodName(method))) return null;
  return { conversationId: sessionId, key: route.key };
}

/** Whether a change captured for `route` would still go there through `method`. */
export function hostChangeRouteCurrent(route: HostChangeRoute, method: Method): boolean {
  return hostChangeRoute(route.conversationId, method)?.key === route.key;
}

export function resetPullRequestPageRoutesForTests(): void {
  decidedRoutes.clear();
}
