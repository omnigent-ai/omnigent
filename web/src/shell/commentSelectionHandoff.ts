// A selection to comment on, handed from a view that can't host comments (the Changes review) to
// the file's viewer, which applies it once its text has loaded.

import type { ActiveSelection } from "./codeViewerHelpers";
import { anchorStartIn } from "./commentAnchoring";
import type { FilePosition } from "./FileViewerContext";
import { selectedLineRange } from "./reviewComments";

/** A selection in `path`'s text, as it was when it was made, for one visit of that file. */
export interface CommentSelectionRequest {
  /** Fresh per request, so taking an older one never consumes a newer one. */
  id: string;
  path: string;
  start_index: number;
  end_index: number;
  anchor_content: string;
  /** The file's text the offsets were taken in. */
  text: string;
}

export type CommentSelectionOutcome =
  | { kind: "apply"; selection: ActiveSelection }
  /** The selected text isn't in the file as it is now. */
  | { kind: "moved" };

/** The selection to make active in `content`, the file as the viewer has it now. */
export function resolveCommentSelection(
  request: CommentSelectionRequest,
  content: string,
): CommentSelectionOutcome {
  const { anchor_content: anchor } = request;
  const start =
    content === request.text
      ? request.start_index
      : anchor
        ? anchorStartIn(content, anchor, request.start_index)
        : -1;
  const end = start + anchor.length;
  // The snapshot's offsets or a remapped start may not select the anchor in this text: check.
  if (!anchor || start < 0 || end > content.length || content.slice(start, end) !== anchor) {
    return { kind: "moved" };
  }
  return {
    kind: "apply",
    selection: { start_index: start, end_index: end, anchor_content: anchor },
  };
}

/** The lines a request selects, cited so the viewer opens the file's source on them. */
export function commentSelectionLines(request: CommentSelectionRequest): FilePosition | undefined {
  // Lines keep their line breaks, as the review's current-version lines do.
  const lines = request.text.split(/(?<=\n)/);
  const range = selectedLineRange(lines, request.start_index, request.end_index);
  if (!range) return undefined;
  return range.end > range.start
    ? { line: range.start, endLine: range.end }
    : { line: range.start };
}
