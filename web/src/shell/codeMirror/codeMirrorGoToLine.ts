// Go to line in a CodeMirror document: previewing a line while the reader
// types, jumping there, and putting the view back on cancel.

import type { ChangeDesc, EditorSelection } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import type { LineTarget } from "@/lib/lineTarget";

/** Where the reader was when Go to line opened, to return to on cancel. */
export interface GoToLineOrigin {
  scrollTop: number;
  selection: EditorSelection;
}

export function goToLineOrigin(view: EditorView): GoToLineOrigin {
  return { scrollTop: view.scrollDOM.scrollTop, selection: view.state.selection };
}

/** Follows edits and refreshes made while Go to line is open, so cancelling stays inside the file. */
export function mapGoToLineOrigin(origin: GoToLineOrigin, changes: ChangeDesc): GoToLineOrigin {
  return { ...origin, selection: origin.selection.map(changes) };
}

/** The document position of `target`, its column kept within the line. */
export function lineTargetPosition(view: EditorView, target: LineTarget): number {
  const line = view.state.doc.line(Math.min(target.line, view.state.doc.lines));
  return line.from + Math.min((target.column ?? 1) - 1, line.length);
}

/** Centers the line without moving the cursor, as the reader types. */
export function previewLine(view: EditorView, target: LineTarget): void {
  view.dispatch({
    effects: EditorView.scrollIntoView(lineTargetPosition(view, target), { y: "center" }),
  });
}

/** Moves the cursor to `target`, centers it, and returns focus to the text. */
export function goToLine(view: EditorView, target: LineTarget): void {
  const position = lineTargetPosition(view, target);
  view.dispatch({
    selection: { anchor: position },
    effects: EditorView.scrollIntoView(position, { y: "center" }),
    userEvent: "select",
  });
  view.focus();
}

/** Puts the cursor and scroll position back as they were. */
export function returnToOrigin(view: EditorView, origin: GoToLineOrigin): void {
  view.dispatch({ selection: origin.selection });
  view.scrollDOM.scrollTop = origin.scrollTop;
  view.focus();
}
