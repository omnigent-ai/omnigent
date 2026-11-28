// Keys that name an Activity entry, shared by its row and anything that opens it.

import type {
  PullRequestComment,
  PullRequestTimelineEvent,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { listKeys } from "@/lib/listKeys";

/**
 * Each comment's key, unique even where a forge gives every comment in a thread the same link,
 * or several reviews' bodies all link to the pull request.
 */
export function commentActivityIds(comments: readonly PullRequestComment[]) {
  return listKeys(comments, (comment) => [
    comment.url,
    comment.created_at,
    comment.author_id ?? comment.author,
  ]).map((key) => `comment:${key}`);
}

/** How a posted comment is recognized once the PR's comments include it. */
export function postedCommentKey(comment: PullRequestComment) {
  return JSON.stringify([comment.url, comment.created_at]);
}

export function timelineEventActivityId(event: PullRequestTimelineEvent) {
  return `event:${event.id}`;
}
