import type { Comment } from "@/hooks/useComments";
import type { ReviewThreadAnchor } from "./pullRequestReviewThreads";

export type LocalReviewAnchor = Omit<ReviewThreadAnchor, "path"> & { pr_url: string };

/** Include snapshots as well as IDs so native agents need no comment-fetch tool. */
export function formatLocalReviewComments(comments: Comment[]): string {
  return [
    "Please address these local review comments:",
    ...comments.map((comment) => {
      const anchor = comment.review_anchor!;
      const range = anchor.start_line ? `${anchor.start_line}–${anchor.line}` : `${anchor.line}`;
      return `\n[Comment ${comment.id}] ${comment.path} (${anchor.side === "LEFT" ? "old" : "new"} lines ${range})\nPR: ${anchor.pr_url}\nBase: ${anchor.base_sha}; head: ${anchor.head_sha}\n${comment.body}`;
    }),
  ].join("\n");
}
