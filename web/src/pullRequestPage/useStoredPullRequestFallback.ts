// What a pull request's own page shows while the session's runner and host are offline:
// the server's stored copy of that PR, as the Pull requests panel shows it.

import type { PullRequestAssociation, usePullRequestInfo } from "@/hooks/usePullRequests";
import { storesSessionPullRequests, useSessionPullRequests } from "@/hooks/useSessionPullRequests";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { pullRequestKey } from "@/lib/pullRequestReference";
import { useHostOffline } from "@/shell/PullRequestPanel";

export interface StoredPullRequestFallback {
  /** The server's copy of the PR while offline. */
  storedPr?: PullRequestAssociation;
  /** Offline, with no copy, before the server imported the session's PRs. */
  notSynced: boolean;
}

/**
 * The stored copy of `prUrl` while the host is offline, matched however either link is
 * cased; nothing while the host can answer, or on a server that doesn't store PRs.
 */
export function useStoredPullRequestFallback(
  conversationId: string,
  info: Pick<ReturnType<typeof usePullRequestInfo>, "data" | "error">,
  prUrl: string,
): StoredPullRequestFallback {
  const storesPrs = storesSessionPullRequests(useServerInfo());
  const stored = useSessionPullRequests(conversationId, info.data);
  const hostOffline = useHostOffline(conversationId, info);
  if (!storesPrs || !hostOffline) return { storedPr: undefined, notSynced: false };
  const key = pullRequestKey(prUrl);
  const storedPr = stored.data?.prs.find((pr) => pullRequestKey(pr.url) === key);
  return { storedPr, notSynced: !storedPr && stored.data?.synced === false };
}
