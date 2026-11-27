// The Merge section's merge queue: read where the session's host serves GetMergeQueue, and
// changed where it serves EnqueuePullRequest or DequeuePullRequest. Unserved, nothing shows.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  dequeueOnHost,
  enqueueOnHost,
  type MergeQueueState,
  readMergeQueue,
} from "@/lib/hostMergeQueue";
import { sendStateChange } from "@/lib/pullRequestStateHolds";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

export const mergeQueueKey = (conversationId: string, prUrl: string) =>
  ["pr-page-github-merge-queue", conversationId, prUrl] as const;

/** The PR's queue state where the host reads it; null where it isn't served or can't say. */
export function useMergeQueue(
  conversationId: string,
  prUrl: string,
  wanted: boolean,
): MergeQueueState | null {
  const served = usePullRequestMethodServed(conversationId, "getMergeQueue");
  const query = useQuery({
    queryKey: mergeQueueKey(conversationId, prUrl),
    queryFn: ({ signal }) => readMergeQueue(conversationId, prUrl, signal),
    enabled: served && wanted,
    retry: false,
    staleTime: 30_000,
  });
  return served && wanted ? (query.data ?? null) : null;
}

/** Add to and remove from the queue, each offered only where the host serves it. */
export function useMergeQueueChanges(conversationId: string, prUrl: string) {
  const queryClient = useQueryClient();
  const addServed = usePullRequestMethodServed(conversationId, "enqueuePullRequest");
  const removeServed = usePullRequestMethodServed(conversationId, "dequeuePullRequest");
  const settle = {
    onSuccess: (state: MergeQueueState) =>
      queryClient.setQueryData(mergeQueueKey(conversationId, prUrl), state),
    // A refusal or an unknown outcome: read where it stands now.
    onError: () =>
      void queryClient.invalidateQueries({ queryKey: mergeQueueKey(conversationId, prUrl) }),
  };
  const add = useMutation({
    // The queue changes the PR's state, so it waits for a merge or close in flight, and they for it.
    mutationFn: (expectedHead: string) =>
      sendStateChange(prUrl, "pull_request_merge_queue_add", () =>
        enqueueOnHost(conversationId, prUrl, expectedHead),
      ),
    ...settle,
  });
  const remove = useMutation({
    mutationFn: () =>
      sendStateChange(prUrl, "pull_request_merge_queue_remove", () =>
        dequeueOnHost(conversationId, prUrl),
      ),
    ...settle,
  });
  return { add: addServed ? add : null, remove: removeServed ? remove : null };
}
