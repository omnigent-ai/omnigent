// The Merge section's merge queue: read where the session's host serves GetMergeQueue, and
// changed where it serves EnqueuePullRequest or DequeuePullRequest. Unserved, nothing shows.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  dequeueOnHost,
  enqueueOnHost,
  type MergeQueueState,
  readMergeQueue,
} from "@/lib/hostMergeQueue";
import {
  confirmQueueChangeChecked,
  markQueueChangeUnconfirmed,
  type MergeQueueWriteOperation,
  sendStateChange,
  unconfirmedQueueChange,
} from "@/lib/pullRequestStateHolds";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

export const mergeQueueKey = (conversationId: string, prUrl: string) =>
  ["pr-page-github-merge-queue", conversationId, prUrl] as const;

/** Read the queue; an answer to a read begun after a lost change settles that change. */
async function readQueueSettling(conversationId: string, prUrl: string, signal: AbortSignal) {
  const owner = draftOwner();
  const unconfirmed = unconfirmedQueueChange(prUrl);
  const state = await readMergeQueue(conversationId, prUrl, signal);
  if (state && unconfirmed) confirmQueueChangeChecked(owner, prUrl, unconfirmed.revision);
  return state;
}

/**
 * Send a queue change, held with the PR's other state changes. One whose answer was lost holds
 * them until a queue read begun after it answers.
 */
function sendQueueChange(
  prUrl: string,
  operation: MergeQueueWriteOperation,
  send: () => Promise<MergeQueueState>,
): Promise<MergeQueueState> {
  // The user who sends it owns a lost answer's hold, though another signs in before it's lost.
  const owner = draftOwner();
  return sendStateChange(prUrl, operation, async () => {
    try {
      return await send();
    } catch (error) {
      // The host makes it as its own account, so no login of ours names who made it.
      if (error instanceof PullRequestWriteError && error.outcomeUnknown)
        markQueueChangeUnconfirmed(owner, prUrl, operation, { login: "" });
      throw error;
    }
  });
}

/** The PR's queue state where the host reads it; null where it isn't served or can't say. */
export function useMergeQueue(
  conversationId: string,
  prUrl: string,
  wanted: boolean,
): MergeQueueState | null {
  const served = usePullRequestMethodServed(conversationId, "getMergeQueue");
  const query = useQuery({
    queryKey: mergeQueueKey(conversationId, prUrl),
    queryFn: ({ signal }) => readQueueSettling(conversationId, prUrl, signal),
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
      sendQueueChange(prUrl, "pull_request_merge_queue_add", () =>
        enqueueOnHost(conversationId, prUrl, expectedHead),
      ),
    ...settle,
  });
  const remove = useMutation({
    mutationFn: () =>
      sendQueueChange(prUrl, "pull_request_merge_queue_remove", () =>
        dequeueOnHost(conversationId, prUrl),
      ),
    ...settle,
  });
  return { add: addServed ? add : null, remove: removeServed ? remove : null };
}
