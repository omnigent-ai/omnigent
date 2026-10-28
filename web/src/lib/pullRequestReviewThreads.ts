import type { DiffLineAnnotation, FileDiffMetadata } from "@pierre/diffs";

export interface ReviewComment {
  id: string;
  author: string | null;
  body: string;
  created_at: string | null;
  url: string | null;
  minimized?: boolean;
}

export interface ReviewPage<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
  warning?: string;
}

export interface ReviewThread {
  id: string;
  path: string;
  side: "LEFT" | "RIGHT";
  line: number | null;
  start_line: number | null;
  start_side?: "LEFT" | "RIGHT" | null;
  original_line?: number | null;
  subject_type: "LINE" | "FILE";
  resolved: boolean;
  outdated: boolean;
  can_reply: boolean;
  diff_hunk: string;
  comments: ReviewPage<ReviewComment>;
}

export interface ReviewThreadsPage extends ReviewPage<ReviewThread> {
  head_sha: string;
  base_sha: string;
}

/** Keep identity separate from position: several conversations can share a line. */
export function reviewThreadAnnotations(
  threads: ReviewThread[],
  fileDiff: FileDiffMetadata | undefined,
  matchingRevision: boolean,
): { annotations: DiffLineAnnotation<ReviewThread[]>[]; unplaced: ReviewThread[] } {
  const annotations = new Map<string, DiffLineAnnotation<ReviewThread[]>>();
  const unplaced: ReviewThread[] = [];
  for (const thread of threads) {
    const side = thread.side === "LEFT" ? "deletions" : "additions";
    const line = thread.line;
    const inHunk = fileDiff?.hunks.some((hunk) => {
      const start = side === "deletions" ? hunk.deletionStart : hunk.additionStart;
      const count = side === "deletions" ? hunk.deletionCount : hunk.additionCount;
      return line !== null && line >= start && line < start + count;
    });
    if (
      !matchingRevision ||
      !fileDiff ||
      thread.outdated ||
      thread.subject_type === "FILE" ||
      !line ||
      !inHunk ||
      (thread.side !== "LEFT" && thread.side !== "RIGHT")
    ) {
      unplaced.push(thread);
      continue;
    }
    const key = `${side}:${line}`;
    const annotation = annotations.get(key);
    if (annotation) annotation.metadata.push(thread);
    else annotations.set(key, { side, lineNumber: line, metadata: [thread] });
  }
  return { annotations: [...annotations.values()], unplaced };
}

export function mergeReviewComments(
  current: ReviewComment[],
  incoming: ReviewComment[],
): ReviewComment[] {
  const comments = new Map(current.map((comment) => [comment.id, comment]));
  for (const comment of incoming) comments.set(comment.id, comment);
  return [...comments.values()].sort((left, right) =>
    (left.created_at ?? "").localeCompare(right.created_at ?? ""),
  );
}

export function appendReviewReply(
  page: ReviewThreadsPage,
  threadId: string,
  comment: ReviewComment,
): ReviewThreadsPage {
  return {
    ...page,
    data: page.data.map((thread) =>
      thread.id === threadId
        ? {
            ...thread,
            comments: {
              ...thread.comments,
              data: mergeReviewComments(thread.comments.data, [comment]),
            },
          }
        : thread,
    ),
  };
}
