import type { QueryClient } from "@tanstack/react-query";

import { refreshPullRequestFastTrack } from "@/hooks/usePullRequestCommits";
import { refreshMergeReadiness } from "@/hooks/useMergeReadiness";
import { refreshPullRequestStackMembers } from "@/hooks/usePullRequestStackMembers";
import { clearSessionHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { beginFreshReads, EVERY_PULL_REQUEST } from "@/lib/pullRequestFreshReads";
import { invalidateSessionPullRequestReads } from "@/lib/pullRequestRecord";
import { sessionPullRequestsKey } from "@/newPullRequestPage/hooks/useSessionPullRequests";

/** The PR reads the Pull requests view shows. */
const SESSION_PULL_REQUEST_READS = [
  "pr-page-github-info",
  "pr-page-github-review-threads",
  "pr-page-github-changed-files",
  "pr-page-github-pr-diff",
  "pr-page-github-pr-commits",
  "pr-page-github-pr-commit-diff",
] as const;

/**
 * Refetch everything a session's Pull requests view shows, as its Refresh button
 * does: the session's PR list and details, and the shared stats, merge readiness and stack members.
 */
export async function refreshSessionPullRequests(
  queryClient: QueryClient,
  sessionId: string,
): Promise<void> {
  // Asking again starts a fresh recovery episode, even after one ran out.
  clearSessionHostTransportRecovery(sessionId);
  // What this Mac kept for any PR doesn't answer reads made for the refresh.
  const endFreshReads = beginFreshReads(EVERY_PULL_REQUEST);
  try {
    await Promise.all([
      invalidateSessionPullRequestReads(queryClient, sessionId, SESSION_PULL_REQUEST_READS),
      queryClient.invalidateQueries({ queryKey: sessionPullRequestsKey(sessionId) }),
      refreshPullRequestFastTrack(queryClient).then(() => undefined),
      refreshMergeReadiness(queryClient),
      refreshPullRequestStackMembers(queryClient),
    ]);
  } finally {
    endFreshReads();
  }
}
