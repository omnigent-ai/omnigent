// A pull request view with no session behind it, such as the Pull Requests page showing a PR
// no session of the view links. A PR there reads through the session that links it, when one
// is known; it has no host of its own, so nothing else it shows is asked of one.

import { createContext, useContext } from "react";

import type { SourceSession } from "@/pullRequestList/subAgentPullRequests";

export interface SessionFreePullRequestScope {
  /** The ID such a view reads under, never a real session's. */
  id: string;
  /** The session that links a PR, whose host reads it; undefined when none is known. */
  linkingSessionOf?: (prUrl: string) => SourceSession | undefined;
}

export const SessionFreePullRequestScopeContext = createContext<SessionFreePullRequestScope | null>(
  null,
);

/** The session-free view around this hook, when `conversationId` is its scope; else null. */
export function useSessionFreePullRequestScope(
  conversationId: string | null | undefined,
): SessionFreePullRequestScope | null {
  const scope = useContext(SessionFreePullRequestScopeContext);
  return scope !== null && scope.id === conversationId ? scope : null;
}

/** Whether `conversationId` is the scope of a session-free pull request view around this hook. */
export function useIsSessionFreePullRequestScope(
  conversationId: string | null | undefined,
): boolean {
  return useSessionFreePullRequestScope(conversationId) !== null;
}
