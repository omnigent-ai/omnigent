// Comment highlights and selection anchors in the CodeMirror viewer. Comments
// anchor to raw file offsets, which the raw document maps to positions.

import {
  type EditorState,
  type Extension,
  type Range,
  StateEffect,
  StateField,
} from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView } from "@codemirror/view";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "../codeViewerHelpers";
import { commentHighlights } from "../commentHighlights";
import { rawDocument } from "./rawOffsets";

export const COMMENT_CLASS = "cm-comment";
export const ACTIVE_COMMENT_CLASS = "cm-comment-active";

export interface CommentLayerState {
  comments: readonly Comment[];
  activeSelection: ActiveSelection | null;
}

const commentMark = Decoration.mark({ class: COMMENT_CLASS });
const activeCommentMark = Decoration.mark({ class: ACTIVE_COMMENT_CLASS });

/** Replaces the comments and active selection the viewer highlights. */
export const setCommentLayer = StateEffect.define<CommentLayerState>();

function highlightDecorations(state: EditorState, layer: CommentLayerState): DecorationSet {
  const { offsets } = state.field(rawDocument);
  const length = state.doc.length;
  const ranges: Range<Decoration>[] = [];
  for (const { start, end, active } of commentHighlights(layer.comments, layer.activeSelection)) {
    const from = Math.min(offsets.toDocument(start), length);
    const to = Math.min(offsets.toDocument(end), length);
    if (to > from) ranges.push((active ? activeCommentMark : commentMark).range(from, to));
  }
  return Decoration.set(ranges, true);
}

interface CommentLayerValue {
  layer: CommentLayerState;
  decorations: DecorationSet;
}

/** Highlights comments, recomputed whenever they or the raw file change. */
export function commentHighlighting(initial: () => CommentLayerState): Extension {
  const field = StateField.define<CommentLayerValue>({
    create: (state) => {
      const layer = initial();
      return { layer, decorations: highlightDecorations(state, layer) };
    },
    update(value, transaction) {
      let layer = value.layer;
      for (const effect of transaction.effects) {
        if (effect.is(setCommentLayer)) layer = effect.value;
      }
      const rawChanged =
        transaction.state.field(rawDocument) !== transaction.startState.field(rawDocument);
      if (layer !== value.layer || rawChanged) {
        return { layer, decorations: highlightDecorations(transaction.state, layer) };
      }
      return transaction.docChanged
        ? { layer, decorations: value.decorations.map(transaction.changes) }
        : value;
    },
    provide: (self) => EditorView.decorations.from(self, (value) => value.decorations),
  });
  return field;
}

/** The raw file offset of a document position. */
export function rawOffsetAt(state: EditorState, position: number): number {
  return state.field(rawDocument).offsets.toRaw(position);
}

/** The document position of a raw file offset, clamped to the document. */
export function positionOfRawOffset(state: EditorState, offset: number): number {
  return Math.min(state.field(rawDocument).offsets.toDocument(offset), state.doc.length);
}

/** The main selection as a comment anchor in raw offsets, or null when it has no text. */
export function selectionAnchor(state: EditorState): ActiveSelection | null {
  const { from, to } = state.selection.main;
  if (from === to) return null;
  const raw = state.field(rawDocument);
  const start = raw.offsets.toRaw(from);
  const end = raw.offsets.toRaw(to);
  const anchor = raw.text.slice(start, end);
  if (!anchor.trim()) return null;
  return { start_index: start, end_index: end, anchor_content: anchor };
}

/**
 * The 1-based lines the main selection covers. A selection that ends at the
 * start of a line doesn't include that line.
 */
export function selectionLineRange(state: EditorState): { start: number; end: number } | null {
  const { from, to } = state.selection.main;
  if (from === to) return null;
  const start = state.doc.lineAt(from);
  const end = state.doc.lineAt(to);
  const endLine = to === end.from && end.number > start.number ? end.number - 1 : end.number;
  return { start: start.number, end: endLine };
}
