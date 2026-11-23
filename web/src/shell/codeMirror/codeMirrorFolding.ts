// Folding by indentation, the way code editors fold languages without a
// syntax-aware provider: a line folds the block of deeper-indented lines after it.

import {
  codeFolding,
  foldedRanges,
  foldEffect,
  foldService,
  unfoldEffect,
} from "@codemirror/language";
import type { EditorState, Extension, Line } from "@codemirror/state";
import { type EditorView, gutter, GutterMarker, type ViewUpdate } from "@codemirror/view";

function indentationOf(text: string, tabSize: number): number | null {
  let columns = 0;
  for (const character of text) {
    if (character === " ") columns += 1;
    else if (character === "\t") columns += tabSize - (columns % tabSize);
    else return columns;
  }
  // Blank lines take the indentation of their surroundings.
  return null;
}

/** The foldable range starting at the line at `lineStart`, or null when nothing is nested under it. */
export function indentationFoldRange(
  state: EditorState,
  lineStart: number,
): { from: number; to: number } | null {
  const tabSize = state.tabSize;
  const line = state.doc.lineAt(lineStart);
  const indent = indentationOf(line.text, tabSize);
  if (indent === null) return null;
  let lastNested = line.number;
  for (let number = line.number + 1; number <= state.doc.lines; number += 1) {
    const nestedIndent = indentationOf(state.doc.line(number).text, tabSize);
    if (nestedIndent === null) continue;
    if (nestedIndent <= indent) break;
    lastNested = number;
  }
  if (lastNested === line.number) return null;
  return { from: line.to, to: state.doc.line(lastNested).to };
}

/**
 * Whether `line` opens an indented block: its next non-blank line is deeper.
 * The gutter asks this for every visible line on each edit, so it reads only
 * up to that next line; the block's end is found only when it is folded.
 */
export function opensIndentedBlock(state: EditorState, line: Line): boolean {
  const indent = indentationOf(line.text, state.tabSize);
  if (indent === null) return false;
  for (let number = line.number + 1; number <= state.doc.lines; number += 1) {
    const nestedIndent = indentationOf(state.doc.line(number).text, state.tabSize);
    if (nestedIndent !== null) return nestedIndent > indent;
  }
  return false;
}

/** The folded range that hangs off the end of `line`, if it is folded. */
function foldAfter(state: EditorState, line: Line): { from: number; to: number } | null {
  let folded: { from: number; to: number } | null = null;
  foldedRanges(state).between(line.to, line.to, (from, to) => {
    if (from === line.to) folded = { from, to };
  });
  return folded;
}

class FoldMarker extends GutterMarker {
  readonly open: boolean;

  constructor(open: boolean) {
    super();
    this.open = open;
  }

  override eq(other: GutterMarker): boolean {
    return other instanceof FoldMarker && other.open === this.open;
  }

  override toDOM(): HTMLElement {
    const marker = document.createElement("span");
    marker.className = this.open ? "cm-foldMarker" : "cm-foldMarker cm-foldMarker-folded";
    marker.textContent = this.open ? "⌄" : "›";
    return marker;
  }
}

const OPEN_MARKER = new FoldMarker(true);
const FOLDED_MARKER = new FoldMarker(false);

function foldsChanged(update: ViewUpdate): boolean {
  return update.transactions.some((transaction) =>
    transaction.effects.some((effect) => effect.is(foldEffect) || effect.is(unfoldEffect)),
  );
}

/** Click a marker to fold the block under its line, or unfold it. */
function toggleFoldAt(view: EditorView, lineStart: number): boolean {
  const line = view.state.doc.lineAt(lineStart);
  const folded = foldAfter(view.state, line);
  if (folded) {
    view.dispatch({ effects: unfoldEffect.of(folded) });
    return true;
  }
  const range = indentationFoldRange(view.state, line.from);
  if (!range) return false;
  view.dispatch({ effects: foldEffect.of(range) });
  return true;
}

const indentationFoldGutter = gutter({
  class: "cm-foldGutter",
  lineMarker(view, block) {
    const line = view.state.doc.lineAt(block.from);
    if (foldAfter(view.state, line)) return FOLDED_MARKER;
    return opensIndentedBlock(view.state, line) ? OPEN_MARKER : null;
  },
  lineMarkerChange: (update) => update.docChanged || foldsChanged(update),
  initialSpacer: () => OPEN_MARKER,
  domEventHandlers: {
    click: (view, block) => toggleFoldAt(view, block.from),
  },
});

/** Indentation folding with a gutter of fold markers. */
export function indentationFolding(): Extension {
  return [
    foldService.of((state, lineStart) => indentationFoldRange(state, lineStart)),
    codeFolding({ placeholderText: "…" }),
    indentationFoldGutter,
  ];
}
