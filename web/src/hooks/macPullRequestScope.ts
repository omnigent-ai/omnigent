// A pull request view with no session behind it, such as the Pull Requests page showing a PR
// no session links. It reads and changes the PR on this Mac as its own GitHub account, so
// session permissions don't apply: GitHub decides what that account may do. It has no
// host either, so nothing it shows is asked of one.

import { createContext, useContext } from "react";

/** The ID such a view reads under, never a real session's; null outside one. */
export const MacPullRequestScopeContext = createContext<string | null>(null);

/** Whether `conversationId` is the scope of a session-free pull request view around this hook. */
export function useIsMacPullRequestScope(conversationId: string | null | undefined): boolean {
  const scope = useContext(MacPullRequestScopeContext);
  return scope !== null && scope === conversationId;
}

/** A PR in a session-free view that this Mac doesn't read; no host could read it instead. */
export class NoSessionReadError extends Error {
  constructor() {
    super("This Mac doesn’t read this pull request. Open it from a session that links it.");
    this.name = "NoSessionReadError";
  }
}
