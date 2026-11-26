// Comments in the whole-file diff review. Comments anchor by character offset
// into a file's current text, which is the review's current-version side. The
// viewer's active selection belongs to its current file.

import type { ReviewTextHighlight } from "@/components/diff-review/useReviewTextLayer";
import type { Comment } from "@/hooks/useComments";
import { positionAt } from "@/components/diff-review/textAnchors";
import type { ActiveSelection } from "./codeViewerHelpers";
import { classifyAndRemapComments } from "./commentAnchoring";
import { commentHighlights, commentSelectionAt } from "./commentHighlights";
import type { FormattedCommentRemap } from "./useMarkdownCommentRemap";

/** Open comments by path. The current file uses the viewer's own list so both agree. */
export function openCommentsByPath({
  comments,
  currentPath,
  currentComments,
  currentText,
  remapFormatted,
}: {
  comments: readonly Comment[];
  currentPath: string;
  currentComments: readonly Comment[];
  currentText: (path: string) => string | undefined;
  /** As in `classifyAndRemapComments`: undefined keeps formatted spans while it loads. */
  remapFormatted: FormattedCommentRemap | null | undefined;
}): ReadonlyMap<string, readonly Comment[]> {
  const byPath = new Map<string, Comment[]>();
  for (const comment of comments) {
    if (comment.path === currentPath) continue;
    const list = byPath.get(comment.path) ?? [];
    list.push(comment);
    byPath.set(comment.path, list);
  }
  const open = new Map<string, readonly Comment[]>();
  for (const [path, list] of byPath) {
    open.set(path, classifyAndRemapComments(list, currentText(path) ?? "", remapFormatted).open);
  }
  open.set(currentPath, currentComments);
  return open;
}

/** Every open comment as a highlight; only the current file has an active selection. */
export function reviewCommentHighlights(
  comments: ReadonlyMap<string, readonly Comment[]>,
  currentPath: string,
  activeSelection: ActiveSelection | null,
): ReviewTextHighlight[] {
  return [...comments].flatMap(([path, list]) =>
    commentHighlights(list, path === currentPath ? activeSelection : null).map((highlight) => ({
      path,
      ...highlight,
    })),
  );
}

/** The selection that activates the open comment covering `offset`, with its id. */
export function reviewCommentSelectionAt(
  comments: readonly Comment[] | undefined,
  offset: number,
): ActiveSelection | undefined {
  const selection = comments && commentSelectionAt(comments, offset);
  if (!selection) return undefined;
  const comment = comments.find(
    (candidate) =>
      candidate.start_index === selection.start_index &&
      candidate.end_index === selection.end_index,
  );
  return { ...selection, comment_id: comment?.id };
}

/**
 * The lines a selection covers, for attaching it to the agent. A selection
 * that ends at the start of a line doesn't include that line.
 */
export function selectedLineRange(
  lines: readonly string[],
  start: number,
  end: number,
): { start: number; end: number } | undefined {
  const from = positionAt(lines, start);
  const to = positionAt(lines, end);
  if (!from || !to) return undefined;
  const endLine = to.column === 0 && to.line > from.line ? to.line - 1 : to.line;
  return { start: from.line, end: endLine };
}
