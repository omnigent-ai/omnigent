// The PR details a refresh belongs to: their parts (the stack's members and comparison) take part
// in the refreshes those details ask for, and in no other view's of the same pull request.

import { createContext, useContext } from "react";

import type { PullRequestRefreshOwner } from "./refreshPullRequest";

export const PullRequestRefreshOwnerContext = createContext<PullRequestRefreshOwner | null>(null);

/** The details this part renders in, or null outside any. */
export function usePullRequestRefreshOwner(): PullRequestRefreshOwner | null {
  return useContext(PullRequestRefreshOwnerContext);
}
