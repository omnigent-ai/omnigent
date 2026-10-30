import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import {
  mergeReviewComments,
  type ReviewComment,
  type ReviewPage,
  type ReviewThread,
  type ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";

interface CachedThreads extends ReviewThreadsPage {
  first_page_ids?: string[];
  paged_comment_ids?: string[];
  history_stale?: boolean;
}

async function fetchReviewPage<T>(
  conversationId: string,
  resource: string,
  params: Record<string, string>,
  signal?: AbortSignal,
): Promise<T> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github/${resource}?${new URLSearchParams(params)}`,
    { signal },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? body.error?.message ?? "Could not load review conversations.");
  }
  return response.json();
}

/** Only retain paginated history and replies not yet observed in a server read. */
function reconcileThreads(firstPage: ReviewThreadsPage, previous?: CachedThreads): CachedThreads {
  const old =
    previous?.head_sha === firstPage.head_sha && previous?.base_sha === firstPage.base_sha
      ? previous
      : undefined;
  if (firstPage.warning && firstPage.data.length === 0 && old) {
    return { ...old, warning: firstPage.warning };
  }
  const firstIds = new Set(firstPage.data.map((thread) => thread.id));
  const oldFirstIds = new Set(old?.first_page_ids ?? []);
  const extra =
    old?.data.filter((thread) => !firstIds.has(thread.id) && !oldFirstIds.has(thread.id)) ?? [];
  const data = firstPage.data.map((thread) => {
    const existing = old?.data.find((value) => value.id === thread.id);
    if (!existing) return thread;
    const paged = old?.paged_comment_ids?.includes(thread.id);
    const pending = existing.pending_comment_ids?.filter(
      (id) => !thread.comments.data.some((comment) => comment.id === id),
    );
    return {
      ...thread,
      pending_comment_ids: pending,
      comments: {
        ...(paged ? existing.comments : thread.comments),
        warning: thread.comments.warning,
        data: mergeReviewComments(
          paged ? existing.comments.data : thread.comments.data,
          paged
            ? thread.comments.data
            : existing.comments.data.filter((comment) => pending?.includes(comment.id)),
        ),
      },
    };
  });
  return {
    ...firstPage,
    data: [...data, ...extra],
    first_page_ids: [...firstIds],
    paged_comment_ids: old?.paged_comment_ids,
    history_stale: !!(extra.length || old?.paged_comment_ids?.length),
    ...(extra.length && old ? { has_more: old.has_more, next_cursor: old.next_cursor } : {}),
  };
}

/** Fetch and poll published threads only while the Diff view is active. */
export function usePullRequestReviewThreads(
  conversationId: string,
  prUrl: string | undefined,
  account: string | null | undefined,
  revision: string,
  enabled: boolean,
) {
  const queryClient = useQueryClient();
  const key = ["github-review-threads", conversationId, prUrl ?? "", account ?? "", revision];
  const query = useQuery<CachedThreads>({
    queryKey: key,
    queryFn: async ({ signal }) => {
      const firstPage = await fetchReviewPage<ReviewThreadsPage>(
        conversationId,
        "review-threads",
        { pr_url: prUrl! },
        signal,
      );
      return reconcileThreads(firstPage, queryClient.getQueryData<CachedThreads>(key));
    },
    enabled: enabled && !!prUrl,
    retry: false,
    refetchInterval: enabled ? 10_000 : false,
    refetchIntervalInBackground: false,
  });
  const page = query.data;
  const moreThreads = useMutation({
    retry: false,
    mutationFn: async (current: ReviewThreadsPage) => ({
      key,
      result: await fetchReviewPage<ReviewThreadsPage>(conversationId, "review-threads", {
        pr_url: prUrl!,
        after: current.next_cursor!,
        head_sha: current.head_sha,
        base_sha: current.base_sha,
      }),
    }),
    onSuccess: ({ result, key: cacheKey }) =>
      queryClient.setQueryData<CachedThreads>(cacheKey, (current) => {
        if (
          !current ||
          current.head_sha !== result.head_sha ||
          current.base_sha !== result.base_sha
        )
          return current;
        const threads = new Map(current.data.map((thread) => [thread.id, thread]));
        for (const thread of result.data) threads.set(thread.id, thread);
        return { ...current, ...result, data: [...threads.values()] };
      }),
  });
  const moreComments = useMutation({
    retry: false,
    mutationFn: async (thread: ReviewThread) => ({
      key,
      result: await fetchReviewPage<ReviewPage<ReviewComment>>(
        conversationId,
        `review-threads/${encodeURIComponent(thread.id)}/comments`,
        { pr_url: prUrl!, after: thread.comments.next_cursor! },
      ),
    }),
    onSuccess: ({ result, key: cacheKey }, thread) =>
      queryClient.setQueryData<CachedThreads>(
        cacheKey,
        (current) =>
          current && {
            ...current,
            paged_comment_ids: [...new Set([...(current.paged_comment_ids ?? []), thread.id])],
            data: current.data.map((value) =>
              value.id === thread.id
                ? {
                    ...value,
                    comments: {
                      ...result,
                      data: mergeReviewComments(value.comments.data, result.data),
                    },
                  }
                : value,
            ),
          },
      ),
  });
  return {
    page,
    isFetching: query.isFetching || moreThreads.isPending || moreComments.isPending,
    historyStale: page?.history_stale,
    refreshHistory: () => void queryClient.resetQueries({ queryKey: key, exact: true }),
    loadMore: () => {
      if (page?.next_cursor && !moreThreads.isPending) moreThreads.mutate(page);
    },
    loadingMore: moreThreads.isPending,
    loadComments: (thread: ReviewThread) => {
      if (thread.comments.next_cursor && !moreComments.isPending) moreComments.mutate(thread);
    },
    loadingComments: moreComments.isPending ? moreComments.variables.id : null,
    error: query.error?.message ?? moreThreads.error?.message ?? moreComments.error?.message,
  };
}
