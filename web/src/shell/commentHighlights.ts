// Which spans of a file the comment layer highlights, shared by every code
// surface that anchors comments to raw character offsets.

import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";

export interface CommentHighlight {
  /** Raw character offset where the highlight starts. */
  start: number;
  /** Raw character offset where the highlight ends, exclusive. */
  end: number;
  /** The active comment, or a selection that isn't saved yet. */
  active: boolean;
}

function spansSelection(comment: Comment, selection: ActiveSelection): boolean {
  return comment.start_index === selection.start_index && comment.end_index === selection.end_index;
}

/** Every saved comment, plus the active selection when it isn't one of them. */
export function commentHighlights(
  comments: readonly Comment[],
  activeSelection: ActiveSelection | null,
): CommentHighlight[] {
  const highlights = comments.map((comment) => ({
    start: comment.start_index,
    end: comment.end_index,
    active: activeSelection != null && spansSelection(comment, activeSelection),
  }));
  if (
    activeSelection != null &&
    activeSelection.end_index > activeSelection.start_index &&
    !comments.some((comment) => spansSelection(comment, activeSelection))
  ) {
    highlights.push({
      start: activeSelection.start_index,
      end: activeSelection.end_index,
      active: true,
    });
  }
  return highlights;
}

/** The selection that activates the comment containing a raw offset, if any. */
export function commentSelectionAt(
  comments: readonly Comment[],
  offset: number,
): ActiveSelection | undefined {
  const comment = comments.find(
    (candidate) => candidate.start_index <= offset && offset < candidate.end_index,
  );
  if (!comment) return undefined;
  return {
    start_index: comment.start_index,
    end_index: comment.end_index,
    anchor_content: comment.anchor_content ?? "",
  };
}
