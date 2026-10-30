import {
  parsePatchFiles,
  type DiffLineAnnotation,
  type FileDiffMetadata,
  type SelectedLineRange,
} from "@pierre/diffs";

export const PULL_REQUEST_DIFF_THEME = { dark: "github-dark", light: "github-light" } as const;

export interface ReviewThreadAnchor {
  path: string;
  side: "LEFT" | "RIGHT";
  line: number;
  start_line: number | null;
  head_sha: string;
  base_sha: string;
}

/** GitHub ranges must stay on one side and within the displayed patch. */
export function reviewSelection(
  range: SelectedLineRange,
  file: FileDiffMetadata,
): Pick<ReviewThreadAnchor, "side" | "line" | "start_line"> | null {
  if (!range.side || (range.endSide && range.endSide !== range.side)) return null;
  const start = Math.min(range.start, range.end);
  const end = Math.max(range.start, range.end);
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1) return null;
  const inHunk = file.hunks.some((hunk) => {
    const offset = range.side === "deletions" ? hunk.deletionStart : hunk.additionStart;
    const count = range.side === "deletions" ? hunk.deletionCount : hunk.additionCount;
    return start >= offset && end < offset + count;
  });
  return inHunk
    ? {
        side: range.side === "deletions" ? "LEFT" : "RIGHT",
        line: end,
        start_line: start === end ? null : start,
      }
    : null;
}

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
  original_start_line?: number | null;
  subject_type: "LINE" | "FILE";
  resolved: boolean;
  outdated: boolean;
  can_reply: boolean;
  diff_hunk: string;
  comments: ReviewPage<ReviewComment>;
  /** Locally posted replies awaiting confirmation by a read. */
  pending_comment_ids?: string[];
}

export interface ReviewThreadsPage extends ReviewPage<ReviewThread> {
  head_sha: string;
  base_sha: string;
}

export interface ReviewSuggestionSource {
  line: number;
  lines: string[];
  original: boolean;
}

/** A suggestion replaces its entire range, including unchanged lines within it. */
export function reviewSuggestionDiff(
  path: string,
  source: ReviewSuggestionSource,
  replacement: string,
): FileDiffMetadata {
  const added = replacement === "" ? [] : replacement.split(/\r?\n/);
  const patch = [
    "diff --git a/suggestion b/suggestion",
    "--- a/suggestion",
    "+++ b/suggestion",
    `@@ -${source.line},${source.lines.length} +${added.length ? source.line : source.line - 1},${added.length} @@`,
    ...source.lines.map((line) => `-${line}`),
    ...added.map((line) => `+${line}`),
    "",
  ].join("\n");
  const diff = parsePatchFiles(patch)[0].files[0];
  diff.name = path;
  // This card contains only the replacement range, without expandable surrounding context.
  diff.hunks[0].collapsedBefore = 0;
  return diff;
}

/** Read only a complete range from one side of a patch or an expanded file. */
function reviewRangeLines(
  file: FileDiffMetadata,
  side: ReviewThread["side"],
  start: number,
  end: number,
): string[] | null {
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start)
    return null;
  const left = side === "LEFT";
  const lines = left ? file.deletionLines : file.additionLines;
  let index = start - 1;
  if (file.isPartial) {
    const hunk = file.hunks.find((value) => {
      const offset = left ? value.deletionStart : value.additionStart;
      const count = left ? value.deletionCount : value.additionCount;
      return start >= offset && end < offset + count;
    });
    if (!hunk) return null;
    index =
      (left ? hunk.deletionLineIndex : hunk.additionLineIndex) +
      start -
      (left ? hunk.deletionStart : hunk.additionStart);
  }
  const selected = lines.slice(index, index + end - start + 1);
  return selected.length === end - start + 1
    ? selected.map((line) => line.replace(/\r?\n$/, ""))
    : null;
}

/** Prefer the displayed revision; label a comment's saved snippet as original context. */
export function reviewSuggestionSource(
  thread: ReviewThread,
  file: FileDiffMetadata | undefined,
): ReviewSuggestionSource | null {
  if (
    thread.subject_type !== "LINE" ||
    (thread.side !== "LEFT" && thread.side !== "RIGHT") ||
    (thread.start_side && thread.start_side !== thread.side)
  )
    return null;
  const end = thread.line;
  if (
    file &&
    !thread.outdated &&
    end !== null &&
    (file.name === thread.path || file.prevName === thread.path)
  ) {
    const start = thread.start_line ?? end;
    const lines = reviewRangeLines(file, thread.side, start, end);
    if (lines) return { line: start, lines, original: false };
  }
  // Older hosts omit the original range, so its length cannot be inferred safely.
  if (!thread.original_line || thread.original_start_line === undefined || !thread.diff_hunk)
    return null;
  try {
    const hunk = thread.diff_hunk.endsWith("\n") ? thread.diff_hunk : `${thread.diff_hunk}\n`;
    const original = parsePatchFiles(
      `diff --git a/review b/review\n--- a/review\n+++ b/review\n${hunk}`,
    )[0]?.files[0];
    if (!original) return null;
    const start = thread.original_start_line ?? thread.original_line;
    const lines = reviewRangeLines(original, thread.side, start, thread.original_line);
    return lines ? { line: start, lines, original: true } : null;
  } catch {
    return null;
  }
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
            pending_comment_ids: [...new Set([...(thread.pending_comment_ids ?? []), comment.id])],
            comments: {
              ...thread.comments,
              data: mergeReviewComments(thread.comments.data, [comment]),
            },
          }
        : thread,
    ),
  };
}
