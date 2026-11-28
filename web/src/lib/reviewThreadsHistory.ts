// How a PR's conversations page changes as reads land: a first page merged with the history
// loaded before it, a later page of threads, and a page of one thread's replies. Each one dates
// what it observed and keeps every other part's read as it was (see reviewThreadsReadAt).

import {
  mergeReviewComments,
  type ReviewComment,
  type ReviewPage,
  type ReviewThread,
  type ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";
import {
  commentsObserved,
  firstPageObserved,
  pageAfter,
  type TimedThreadsPage,
  threadsObserved,
} from "@/lib/reviewThreadsReadAt";

export interface CachedThreads extends TimedThreadsPage {
  first_page_ids?: string[];
  paged_comment_ids?: string[];
  history_stale?: boolean;
}

/** What the next first page merges by: which threads it listed and whose replies were paged. */
export type ThreadsHistory = Pick<
  CachedThreads,
  "first_page_ids" | "paged_comment_ids" | "history_stale"
>;

/** `page`'s merge history, naming only threads it shows, as a save keeps it. */
export function historyOf(page: CachedThreads): ThreadsHistory {
  const shown = new Set(page.data.map((thread) => thread.id));
  const firstIds = page.first_page_ids?.filter((id) => shown.has(id));
  const pagedIds = page.paged_comment_ids?.filter((id) => shown.has(id));
  return {
    ...(firstIds ? { first_page_ids: firstIds } : {}),
    ...(pagedIds?.length ? { paged_comment_ids: pagedIds } : {}),
    ...(page.history_stale ? { history_stale: true } : {}),
  };
}

/**
 * A first page read at `now`, merged with `previous`: only paginated history and replies not yet
 * observed in a server read are retained, each as old as it was.
 */
export function reconcileThreads(
  firstPage: ReviewThreadsPage,
  previous: CachedThreads | undefined,
  now: number,
): CachedThreads {
  const old =
    previous?.head_sha === firstPage.head_sha && previous?.base_sha === firstPage.base_sha
      ? previous
      : undefined;
  // A warning alone observed nothing: what's loaded stays, and as old as it was.
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
    // Nothing of its own to keep: the thread as read, as on the first read, so an unchanged
    // read keeps the cached thread objects and every diff keeps its rows.
    if (!paged && !existing.pending_comment_ids?.length) return thread;
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
  const { reads: _oldReads, ...answer } = firstPage as TimedThreadsPage;
  const merged: CachedThreads = {
    ...answer,
    data: [...data, ...extra],
    first_page_ids: [...firstIds],
    paged_comment_ids: old?.paged_comment_ids,
    history_stale: !!(extra.length || old?.paged_comment_ids?.length),
    ...(extra.length && old ? { has_more: old.has_more, next_cursor: old.next_cursor } : {}),
  };
  return pageAfter(old ?? {}, merged, firstPageObserved(firstPage), now) as CachedThreads;
}

/** `current` with a later page of threads read at `now`; a thread it holds again is replaced. */
export function withMoreThreads(
  current: CachedThreads,
  result: ReviewThreadsPage,
  now: number,
): CachedThreads {
  if (current.head_sha !== result.head_sha || current.base_sha !== result.base_sha) return current;
  const threads = new Map(current.data.map((thread) => [thread.id, thread]));
  for (const thread of result.data) threads.set(thread.id, thread);
  return pageAfter(
    current,
    {
      ...current,
      has_more: result.has_more,
      next_cursor: result.next_cursor,
      ...(result.warning !== undefined ? { warning: result.warning } : {}),
      data: [...threads.values()],
    },
    threadsObserved(result.data),
    now,
  );
}

/** `current` with a page of `thread`'s replies read at `now`. */
export function withMoreComments(
  current: CachedThreads,
  thread: Pick<ReviewThread, "id">,
  result: ReviewPage<ReviewComment>,
  now: number,
): CachedThreads {
  return pageAfter(
    current,
    {
      ...current,
      paged_comment_ids: [...new Set([...(current.paged_comment_ids ?? []), thread.id])],
      data: current.data.map((value) =>
        value.id === thread.id
          ? {
              ...value,
              comments: { ...result, data: mergeReviewComments(value.comments.data, result.data) },
            }
          : value,
      ),
    },
    commentsObserved(thread.id, result.data),
    now,
  );
}

/** `page` with `thread`, as GitHub answered creating it at `now`. */
export function withWrittenThread<Page extends TimedThreadsPage>(
  page: Page,
  thread: ReviewThread,
  now: number,
): Page {
  return pageAfter(
    page,
    { ...page, data: [...page.data.filter((existing) => existing.id !== thread.id), thread] },
    threadsObserved([thread]),
    now,
  );
}

/**
 * A review threads page with `change` made to the comment `commentId` in `threadId`. The answer
 * observed only that comment's body, so every part keeps when it was read; a deleted one goes.
 */
export function withChangedComment(
  page: TimedThreadsPage,
  threadId: string,
  commentId: string,
  change: (comment: ReviewComment) => ReviewComment | null,
): TimedThreadsPage {
  return pageAfter(page, {
    ...page,
    data: page.data.flatMap((thread) => {
      if (thread.id !== threadId) return [thread];
      const data = thread.comments.data.flatMap((comment) => {
        if (comment.id !== commentId) return [comment];
        const changed = change(comment);
        return changed ? [changed] : [];
      });
      // A conversation whose only comment was deleted is gone with it.
      if (data.length === 0 && !thread.comments.has_more) return [];
      return [{ ...thread, comments: { ...thread.comments, data } }];
    }),
  });
}
