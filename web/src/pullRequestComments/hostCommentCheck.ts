// Check Again for a comment change the session's host made but GitHub didn't confirm: read the
// PR again through the session, then say what its reads show of the comment. A comment they
// may simply not list, past the reads' limits, is neither there nor gone: the change stays held.

import type { QueryClient } from "@tanstack/react-query";

import type { NormalizedPullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import { invalidateSessionPullRequest, isSessionPullRequestRead } from "@/lib/pullRequestRecord";
import type { ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";

import { listedDrafts } from "@/pullRequestReviews/pendingReview";
import { hostPendingRoute } from "@/pullRequestReviews/hostPendingRoute";
import { readPendingReviewOnHost } from "@/pullRequestReviews/usePendingReview";

import type { CommentCheckTarget } from "./commentEditStore";

/** How many conversation comments the host's overview lists, the latest ones. */
export const HOST_OVERVIEW_COMMENT_LIMIT = 100;

export type HostCommentState = { state: "present"; body: string } | { state: "absent" };

const isThreadsPage = (data: unknown): data is ReviewThreadsPage =>
  typeof data === "object" &&
  data !== null &&
  Array.isArray((data as ReviewThreadsPage).data) &&
  typeof (data as ReviewThreadsPage).has_more === "boolean";

/** The comment as the session's reads show it once read again; null where they can't say. */
export async function readCommentThroughHost(
  queryClient: QueryClient,
  conversationId: string,
  target: CommentCheckTarget & { threadId?: string },
): Promise<HostCommentState | null> {
  if (target.pendingReviewId) return draftState(conversationId, target, target.pendingReviewId);
  const family = target.kind === "issue" ? "pr-page-github-info" : "pr-page-github-review-threads";
  const checkedFrom = Date.now();
  await invalidateSessionPullRequest(queryClient, conversationId, target.prUrl, [family]);
  // Only reads this check made: a kept read that wasn't read again shows an older revision.
  const reads = queryClient
    .getQueryCache()
    .findAll({
      predicate: ({ queryKey }) =>
        queryKey[0] === family && isSessionPullRequestRead(queryKey, conversationId, target.prUrl),
    })
    .filter(
      ({ state }) =>
        state.status === "success" && !state.isInvalidated && state.dataUpdatedAt >= checkedFrom,
    )
    .map((query) => query.state.data);
  if (target.kind === "issue") return conversationCommentState(reads, target.commentId);
  return reviewCommentState(reads.filter(isThreadsPage), target.commentId, target.threadId);
}

/**
 * A draft as the host's read of the pending review shows it: there with its text, or gone where
 * a complete read of the same review lacks it. Another review, or a partial read, can't say.
 */
async function draftState(
  conversationId: string,
  target: CommentCheckTarget,
  pendingReviewId: string,
): Promise<HostCommentState | null> {
  const route = hostPendingRoute(conversationId, target.prUrl);
  if (!route) return null;
  const read = await readPendingReviewOnHost(route);
  if (read.review?.id !== pendingReviewId) return null;
  const draft = listedDrafts(read).find((listed) => listed.id === target.commentId);
  if (draft) return { state: "present", body: draft.body };
  return read.complete ? { state: "absent" } : null;
}

function conversationCommentState(reads: unknown[], commentId: string): HostCommentState | null {
  let listsEvery = false;
  for (const read of reads) {
    const comments = (read as NormalizedPullRequestInfo | undefined)?.pr?.comments;
    if (!Array.isArray(comments)) continue;
    const found = comments.find((comment) => comment.id === commentId);
    if (found) return { state: "present", body: found.body };
    // Fewer than the limit: the read lists every conversation comment, so it's gone.
    if (comments.length < HOST_OVERVIEW_COMMENT_LIMIT) listsEvery = true;
  }
  return listsEvery ? { state: "absent" } : null;
}

function reviewCommentState(
  pages: ReviewThreadsPage[],
  commentId: string,
  threadId: string | undefined,
): HostCommentState | null {
  let threadComplete = false;
  for (const page of pages) {
    for (const thread of page.data) {
      const found = thread.comments.data.find((comment) => comment.id === commentId);
      if (found) return { state: "present", body: found.body };
      if (thread.id === threadId && !thread.comments.has_more) threadComplete = true;
    }
  }
  if (threadComplete) return { state: "absent" };
  // Its conversation went with its last comment: gone only where one page lists every one.
  const listed = pages.some((page) => page.data.some((thread) => thread.id === threadId));
  const [only, ...more] = pages;
  return !listed && only && more.length === 0 && !only.has_more ? { state: "absent" } : null;
}
