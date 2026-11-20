// The pull request facts the new-tab page's Recent rows show. The linked list is
// the session's own (stored on the server, or live from the host), as the composer
// reads it. Per-PR details are only read from what's already cached, so the page
// never fetches a pull request of its own.

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";

import { linkedPullRequests } from "@/hooks/useComposerGitStatus";
import {
  pullRequestInfoQueryKey,
  usePullRequestInfo,
  type PullRequest,
  type PullRequestInfo,
} from "@/hooks/usePullRequests";
import { useSessionPullRequests } from "@/hooks/useSessionPullRequests";

import type { RecentPullRequestFacts } from "./recentPullRequestRow";

export function useRecentPullRequestFacts(
  conversationId: string,
  urls: readonly string[],
): RecentPullRequestFacts {
  const info = usePullRequestInfo(conversationId, { poll: false });
  const stored = useSessionPullRequests(conversationId, info.data);
  const keys = detailKeys(conversationId, urls);
  const client = useQueryClient();
  // A plain cache subscription, not an observer: an observer would install its own fetch
  // into the shared query, which a later invalidation would then run.
  const subscribe = useCallback(
    (onChange: () => void) => client.getQueryCache().subscribe(onChange),
    [client],
  );
  useSyncExternalStore(subscribe, () => detailsVersion(client, keys));
  const details = new Map<string, PullRequest>();
  if (info.data?.pr) details.set(info.data.pr.url, info.data.pr);
  keys.forEach((key, index) => {
    const pr = client.getQueryData<PullRequestInfo>(key)?.pr;
    if (pr) details.set(urls[index]!, pr);
  });
  const linked = linkedPullRequests(info.data, stored.data);
  return { linked, details };
}

/** Each URL's details key, as the PR's own query names it. */
function detailKeys(conversationId: string, urls: readonly string[]): readonly unknown[][] {
  return urls.map((prUrl) => pullRequestInfoQueryKey(conversationId, prUrl));
}

/** Changes whenever any of the keys' cached data does, so the rows re-render with it. */
function detailsVersion(client: QueryClient, keys: readonly unknown[][]): string {
  return keys.map((key) => client.getQueryState(key)?.dataUpdatedAt ?? 0).join(",");
}
