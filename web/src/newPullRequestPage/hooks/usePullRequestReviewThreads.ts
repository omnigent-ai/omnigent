import { useRef } from "react";
import { hashKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import type { PullRequestFreshnessSource } from "@/lib/pullRequestFreshness";
import { pullRequestRecordKey, sessionRecordOwner } from "@/lib/pullRequestRecord";
import type {
  ReviewComment,
  ReviewPage,
  ReviewThread,
  ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";
import { oldestReadAt } from "@/lib/reviewThreadsReadAt";
import {
  type CachedThreads,
  reconcileThreads,
  withMoreComments,
  withMoreThreads,
} from "@/lib/reviewThreadsHistory";

/** One page of threads, or of one thread's comments, through the session's host. */
async function fetchReviewPage<T>(
  conversationId: string,
  resource: string,
  params: Record<string, string>,
  signal?: AbortSignal,
): Promise<T> {
  const threadId = THREAD_COMMENTS.exec(resource)?.[1];
  const prUrl = params.pr_url ?? "";
  const pageToken = params.after ?? "";
  return readPullRequestRpc<T>(
    conversationId,
    threadId === undefined ? "listReviewThreads" : "listReviewThreadComments",
    (client) =>
      threadId === undefined
        ? client.listReviewThreads(
            { prUrl, pageToken, headSha: params.head_sha ?? "", baseSha: params.base_sha ?? "" },
            { signal },
          )
        : client.listReviewThreadComments(
            { prUrl, threadId: decodeURIComponent(threadId), pageToken },
            { signal },
          ),
    signal,
  );
}

// A thread's comments are read from `review-threads/<id>/comments`.
const THREAD_COMMENTS = /^review-threads\/([^/]+)\/comments$/;

/** Fetch and poll published threads only while the Diff view is active. */
export function usePullRequestReviewThreads(
  conversationId: string,
  prUrl: string | undefined,
  account: string | null | undefined,
  revision: string,
  enabled: boolean,
) {
  const queryClient = useQueryClient();
  // Without a PR nothing is read; the key only keeps the hook's shape.
  const key = prUrl
    ? pullRequestRecordKey(
        "pr-page-github-review-threads",
        prUrl,
        sessionRecordOwner(conversationId),
        account ?? "",
        revision,
      )
    : ["pr-page-github-review-threads", conversationId];
  // Queries whose next successful read replaces the history instead of merging into it, by
  // their exact key: another account's, head's or reader's read never takes this one's.
  // A failed read keeps the loaded threads, as any refetch does.
  const replaceHistoryFor = useRef(new Set<string>());
  const query = useQuery<CachedThreads>({
    queryKey: key,
    queryFn: async ({ signal, queryKey }) => {
      const firstPage = await fetchReviewPage<ReviewThreadsPage>(
        conversationId,
        "review-threads",
        { pr_url: prUrl! },
        signal,
      );
      const previous = queryClient.getQueryData<CachedThreads>(queryKey);
      const identity = hashKey(queryKey);
      // A refresh of the history starts over from the first page, unless this answer is only
      // a warning, which never wipes what's loaded.
      const replacing =
        replaceHistoryFor.current.has(identity) &&
        !(firstPage.warning && firstPage.data.length === 0);
      replaceHistoryFor.current.delete(identity);
      return reconcileThreads(firstPage, replacing ? undefined : previous, Date.now());
    },
    enabled: enabled && !!prUrl,
    // Kept as long as the PR's overview, so a return after a while shows its discussion whole.
    gcTime: 30 * 60_000,
    retry: false,
    // A failed read says so and waits for Refresh, rather than asking again every few seconds.
    refetchInterval: enabled
      ? (current) => (current.state.status === "error" ? false : 10_000)
      : false,
    refetchIntervalInBackground: false,
  });
  const page = query.data;
  const moreThreads = useMutation({
    retry: false,
    mutationFn: async (current: ReviewThreadsPage) => {
      const result = await fetchReviewPage<ReviewThreadsPage>(conversationId, "review-threads", {
        pr_url: prUrl!,
        after: current.next_cursor!,
        head_sha: current.head_sha,
        base_sha: current.base_sha,
      });
      return { key, result, readAt: Date.now() };
    },
    onSuccess: ({ result, key: cacheKey, readAt }) =>
      queryClient.setQueryData<CachedThreads>(
        cacheKey,
        (current) => current && withMoreThreads(current, result, readAt),
      ),
  });
  const moreComments = useMutation({
    retry: false,
    mutationFn: async (thread: ReviewThread) => {
      const result = await fetchReviewPage<ReviewPage<ReviewComment>>(
        conversationId,
        `review-threads/${encodeURIComponent(thread.id)}/comments`,
        { pr_url: prUrl!, after: thread.comments.next_cursor! },
      );
      return { key, result, readAt: Date.now() };
    },
    onSuccess: ({ result, key: cacheKey, readAt }, thread) =>
      queryClient.setQueryData<CachedThreads>(
        cacheKey,
        (current) => current && withMoreComments(current, thread, result, readAt),
      ),
  });
  return {
    page,
    isFetching: query.isFetching || moreThreads.isPending || moreComments.isPending,
    /** How old the shown conversations are, for the freshness mark. */
    freshness: {
      updatedAt: (page && oldestReadAt(page)) ?? query.dataUpdatedAt,
      restored: false,
      refreshing: query.isFetching,
      failed: query.isError,
    } satisfies PullRequestFreshnessSource,
    historyStale: page?.history_stale,
    refreshHistory: () => {
      replaceHistoryFor.current.add(hashKey(key));
      void query.refetch();
    },
    /**
     * Runs again what failed: the first page, the next page of threads, or a thread's next
     * replies, from where it stopped. Loaded history stays; a success clears that failure.
     */
    retry: () => {
      if (query.isError || (!moreThreads.isError && !moreComments.isError)) {
        void query.refetch();
      }
      if (moreThreads.isError) {
        if (page?.next_cursor) moreThreads.mutate(page);
        else moreThreads.reset();
      }
      if (moreComments.isError) {
        // The thread as loaded now, whose cursor is where its replies stopped.
        const failed = page?.data.find((thread) => thread.id === moreComments.variables?.id);
        if (failed?.comments.next_cursor) moreComments.mutate(failed);
        else moreComments.reset();
      }
    },
    loadMore: () => {
      if (page?.next_cursor && !moreThreads.isPending) moreThreads.mutate(page);
    },
    loadingMore: moreThreads.isPending,
    loadComments: (thread: ReviewThread) => {
      if (thread.comments.next_cursor && !moreComments.isPending) moreComments.mutate(thread);
    },
    loadingComments: moreComments.isPending ? moreComments.variables.id : null,
    error: query.error?.message ?? moreThreads.error?.message ?? moreComments.error?.message,
    /** Which read failed, so a notice can say what didn't load. */
    failure: query.error
      ? ("read" as const)
      : moreThreads.error
        ? ("threads" as const)
        : moreComments.error
          ? ("replies" as const)
          : undefined,
  };
}
