// Shows spaces and tabs inside the selection, as code editors do by default,
// so selecting indentation reveals whether it is tabs or spaces.

import { RangeSetBuilder } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  type EditorView,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";

const SPACE = Decoration.mark({ class: "cm-selectionSpace" });
const TAB = Decoration.mark({ class: "cm-selectionTab" });
const WHITESPACE = /[ \t]/g;

function selectionWhitespace(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  const ranges = view.state.selection.ranges
    .filter((range) => !range.empty)
    .map((range) => ({ from: range.from, to: range.to }))
    .sort((first, second) => first.from - second.from);
  // Only the visible lines, so selecting all of a huge file stays cheap.
  for (const visible of view.visibleRanges) {
    for (const range of ranges) {
      const from = Math.max(range.from, visible.from);
      const to = Math.min(range.to, visible.to);
      if (from >= to) continue;
      const text = view.state.sliceDoc(from, to);
      for (const match of text.matchAll(WHITESPACE)) {
        const start = from + (match.index ?? 0);
        builder.add(start, start + 1, match[0] === "\t" ? TAB : SPACE);
      }
    }
  }
  return builder.finish();
}

/** Marks whitespace inside non-empty selections within the visible lines. */
export const selectionWhitespacePlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = selectionWhitespace(view);
    }

    update(update: ViewUpdate) {
      if (update.selectionSet || update.docChanged || update.viewportChanged) {
        this.decorations = selectionWhitespace(update.view);
      }
    }
  },
  { decorations: (plugin) => plugin.decorations },
);
