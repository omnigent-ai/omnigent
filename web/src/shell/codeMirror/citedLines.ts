// The lines a chat citation or search result points at: highlighted while it is
// the viewer's position, and revealed until the reader takes over scrolling.

import { EditorSelection, StateEffect, StateField, type Range } from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView } from "@codemirror/view";
import type { FilePosition } from "../FileViewerContext";

interface CitedRange {
  /** First and last line numbers. */
  from: number;
  to: number;
  /** Document offsets of cited text on the first line, such as a search match. */
  text?: { from: number; to: number };
}

const setCitedLines = StateEffect.define<CitedRange | null>();
const citedLine = Decoration.line({ class: "cm-cited-line" });
const citedText = Decoration.mark({ class: "cm-cited-text" });

export const citedLines = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(lines, transaction) {
    for (const effect of transaction.effects) {
      if (!effect.is(setCitedLines)) continue;
      if (!effect.value) return Decoration.none;
      const { doc } = transaction.state;
      const decorations: Range<Decoration>[] = [];
      for (let number = effect.value.from; number <= effect.value.to; number += 1) {
        decorations.push(citedLine.range(doc.line(number).from));
      }
      const text = effect.value.text;
      if (text) decorations.push(citedText.range(text.from, text.to));
      return Decoration.set(decorations, true);
    }
    return lines.map(transaction.changes);
  },
  provide: (field) => EditorView.decorations.from(field),
});

/** The cited line range clamped to the document, or null without a position. */
export function citedLineRange(
  lineCount: number,
  position: FilePosition | undefined,
): { from: number; to: number } | null {
  if (!position) return null;
  const from = Math.min(Math.max(1, position.line), lineCount);
  const to = Math.min(Math.max(position.endLine ?? from, from), lineCount);
  return { from, to };
}

/** Highlights the cited lines, or clears them when `position` is undefined. */
export function highlightCitedLines(view: EditorView, position: FilePosition | undefined): void {
  const { doc } = view.state;
  const range = citedLineRange(doc.lines, position);
  view.dispatch({
    effects: setCitedLines.of(
      range && { ...range, text: citedTextRange(doc, range.from, position) },
    ),
  });
}

// The text between `column` and `endColumn` on the cited line, clamped to it.
function citedTextRange(
  doc: EditorView["state"]["doc"],
  lineNumber: number,
  position: FilePosition | undefined,
): { from: number; to: number } | undefined {
  if (position?.column === undefined || position.endColumn === undefined) return undefined;
  if (position.line !== lineNumber) return undefined;
  const line = doc.line(lineNumber);
  const from = Math.min(line.from + Math.max(0, position.column - 1), line.to);
  const to = Math.min(line.from + Math.max(0, position.endColumn - 1), line.to);
  return to > from ? { from, to } : undefined;
}

/**
 * Puts the cursor at the cited line and column and centers the range; a range
 * taller than the viewport keeps its first line in view.
 */
export function revealCitedPosition(view: EditorView, position: FilePosition): void {
  const range = citedLineRange(view.state.doc.lines, position);
  if (!range) return;
  const { doc } = view.state;
  const first = doc.line(range.from);
  const last = doc.line(range.to);
  const anchor = Math.min(first.from + Math.max(0, (position.column ?? 1) - 1), first.to);
  view.dispatch({
    selection: { anchor },
    effects: EditorView.scrollIntoView(EditorSelection.range(last.to, first.from), {
      y: "center",
    }),
  });
}
