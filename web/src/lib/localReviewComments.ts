import type { Comment } from "@/hooks/useComments";
import type { ReviewThreadAnchor } from "./pullRequestReviewThreads";

export type LocalReviewAnchor = Omit<ReviewThreadAnchor, "path"> & { pr_url: string };

export const LOCAL_REVIEW_COMMENT_LANGUAGE = "local-review-comment";
const LOCAL_REVIEW_COMMENT_FENCE = `\`\`\`${LOCAL_REVIEW_COMMENT_LANGUAGE}\n`;

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
export function formatLocalReviewComments(
  comments: Comment[],
  { includeInstruction = true }: { includeInstruction?: boolean } = {},
): string {
  return [
    ...(includeInstruction ? ["Please address these local review comments:"] : []),
    ...comments.map((comment) => {
      // Keep quoted attachment markers out of the chat's mention-rewriting pass.
      const snapshot = JSON.stringify(comment, null, 2).replaceAll("[Attached", "\\u005bAttached");
      return `${LOCAL_REVIEW_COMMENT_FENCE}${snapshot}\n\`\`\``;
    }),
  ].join("\n\n");
}

export function parseLocalReviewComment(code: string): Comment | null {
  try {
    const value: unknown = JSON.parse(code);
    return isLocalReviewComment(value) ? value : null;
  } catch {
    return null;
  }
}

/** A collapsed prompt must not expose a snapshot cut in the middle of its JSON. */
export function omitIncompleteLocalReviewComment(text: string): string {
  const start = text.lastIndexOf(LOCAL_REVIEW_COMMENT_FENCE);
  if (start < 0 || text.indexOf("\n```", start + LOCAL_REVIEW_COMMENT_FENCE.length) >= 0) {
    return text;
  }
  return `${text.slice(0, start)}_Expand the prompt to read more review comments._`;
}
