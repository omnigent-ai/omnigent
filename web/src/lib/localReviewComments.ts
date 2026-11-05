import type { Comment } from "@/hooks/useComments";
import type { ReviewThreadAnchor } from "./pullRequestReviewThreads";

export type LocalReviewAnchor = Omit<ReviewThreadAnchor, "path"> & { pr_url: string };

export function isFileComment(comment: Comment): boolean {
  return !comment.anchor_metadata;
}

/** Compare every immutable coordinate, normalizing the optional range start. */
export function sameLocalReviewAnchor(expected: LocalReviewAnchor, actual: LocalReviewAnchor) {
  return (
    expected.pr_url === actual.pr_url &&
    expected.side === actual.side &&
    expected.line === actual.line &&
    (expected.start_line ?? null) === (actual.start_line ?? null) &&
    expected.head_sha === actual.head_sha &&
    expected.base_sha === actual.base_sha
  );
}

/** Validate persisted draft context before using its coordinates or body. */
export function isLocalReviewComment(value: unknown): value is Comment {
  if (!value || typeof value !== "object") return false;
  const comment = value as Partial<Comment>;
  const anchor = comment.anchor_metadata;
  return (
    typeof comment.id === "string" &&
    typeof comment.conversation_id === "string" &&
    typeof comment.path === "string" &&
    typeof comment.body === "string" &&
    comment.status === "draft" &&
    !!anchor &&
    typeof anchor.pr_url === "string" &&
    (anchor.side === "LEFT" || anchor.side === "RIGHT") &&
    Number.isInteger(anchor.line) &&
    anchor.line > 0 &&
    (anchor.start_line == null ||
      (Number.isInteger(anchor.start_line) &&
        anchor.start_line > 0 &&
        anchor.start_line <= anchor.line)) &&
    typeof anchor.head_sha === "string" &&
    typeof anchor.base_sha === "string"
  );
}

/** Include snapshots as well as IDs so native agents need no comment-fetch tool. */
export function formatLocalReviewComments(comments: Comment[]): string {
  return [
    "Please address these local review comments:",
    ...comments.map((comment) => {
      const anchor = comment.anchor_metadata!;
      const range = anchor.start_line ? `${anchor.start_line}–${anchor.line}` : `${anchor.line}`;
      return `\n[Comment ${comment.id}] ${comment.path} (${anchor.side === "LEFT" ? "old" : "new"} lines ${range})\nPR: ${anchor.pr_url}\nBase: ${anchor.base_sha}; head: ${anchor.head_sha}\n${comment.body}`;
    }),
  ].join("\n");
}
