// Shared assembly for the comment side of the inbox — the `/inbox`
// page and the sidebar Inbox badge both need "unseen draft comments
// by other users" across sessions, so the row-filtering + fetching +
// collection lives here once.
//
// Data flow mirrors the approval side (`InboxPage`'s elicitation
// snapshots): session rows already carry a `comments_count` /
// `comments_updated_at` fingerprint kept live by
// `WS /v1/sessions/updates`, so only rows reporting comments mount a
// fetch. Queries reuse `useComments`' ["comments", sessionId] key —
// the cache is shared with FileViewer/CommentsPanel, and
// `SessionUpdatesProvider` invalidates exactly that key whenever a
// row's fingerprint changes, which keeps these lists live without
// any key gymnastics.

import { useQueries } from "@tanstack/react-query";
import { commentsQueryKey, fetchComments } from "@/hooks/useComments";
import type { Conversation } from "@/hooks/useConversations";
import { useSeenCommentIds } from "@/hooks/useSeenComments";
import { getCurrentAuthorId } from "@/lib/identity";
import {
  collectCommentInboxItems,
  type CommentInboxItem,
  type CommentInboxSource,
} from "@/lib/inbox";

export interface CommentInbox {
  /** Unseen draft comments by other users, newest first. */
  items: CommentInboxItem[];
  /** True while any per-session comment fetch is still in flight. */
  isLoading: boolean;
  /** Sessions whose comment fetch failed (their comments are unknown). */
  failedCount: number;
  /** Refetch every failed comment query. */
  retryFailed: () => void;
}

/**
 * Assemble the comment inbox from loaded session rows.
 *
 * :param rows: Session list rows (any pages the caller has loaded).
 *     Archived rows and rows without comments are skipped here, so
 *     callers can pass their list unfiltered.
 */
export function useCommentInbox(rows: Conversation[]): CommentInbox {
  const commentRows = rows.filter((row) => !row.archived && (row.comments_count ?? 0) > 0);
  const queries = useQueries({
    queries: commentRows.map((row) => ({
      queryKey: commentsQueryKey(row.id),
      queryFn: () => fetchComments(row.id),
      // Match useComments so shared-key observers agree on freshness.
      staleTime: 2_000,
      // Absorb a transient blip without hammering a down server —
      // same policy as the inbox's elicitation snapshot queries.
      retry: 1,
    })),
  });
  const seenIds = useSeenCommentIds();

  const sources: CommentInboxSource[] = [];
  commentRows.forEach((row, i) => {
    const comments = queries[i]?.data;
    if (comments) sources.push({ row, comments });
  });

  return {
    items: collectCommentInboxItems(sources, seenIds, getCurrentAuthorId()),
    isLoading: queries.some((q) => q.isLoading),
    failedCount: queries.filter((q) => q.isError).length,
    retryFailed: () => {
      queries.forEach((q) => {
        if (q.isError) void q.refetch();
      });
    },
  };
}
