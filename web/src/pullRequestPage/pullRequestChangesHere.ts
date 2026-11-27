import { createContext, useContext } from "react";

/**
 * Whether the pull request a view shows may be changed from it, through the session's
 * machine or this Mac alike. A PR view sets it from `useCanChangePullRequests`; anywhere
 * else, such as a view of no session, changes stay as their routes allow.
 */
export const PullRequestChangesHereContext = createContext(true);

/** Whether this view may change its pull request ({@link PullRequestChangesHereContext}). */
export function usePullRequestChangesHere(): boolean {
  return useContext(PullRequestChangesHereContext);
}
