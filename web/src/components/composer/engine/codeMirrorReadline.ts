// The macOS readline keys for the CodeMirror composer, over the editor-free boundaries and
// kill ring in lib/textEditing/readline. Off macOS none of these keys are claimed: Ctrl+W,
// Ctrl+A and friends keep their platform meaning there.

import { Annotation, StateField, type Extension, type StateCommand } from "@codemirror/state";
import { EditorView, keymap, type Command, type KeyBinding } from "@codemirror/view";
import {
  EMPTY_KILL_RING,
  endKillRun,
  lineEndAfter,
  lineStartBefore,
  pushKill,
  shellWordStartBefore,
  yankText,
  type KillRing,
} from "@/lib/textEditing/readline";
import { parseReplyQuoteLine } from "@/lib/replyDraftDocument";

/** Marks the text a kill removed, so the kill ring can yank it back. */
export const killed = Annotation.define<{ text: string; backward: boolean }>();

const killRingField = StateField.define<KillRing>({
  create: () => EMPTY_KILL_RING,
  update(ring, transaction) {
    const killedText = transaction.annotation(killed);
    if (killedText) return pushKill(ring, killedText.text, killedText.backward);
    return transaction.docChanged || transaction.selection ? endKillRun(ring) : ring;
  },
});

type Boundary = (text: string, offset: number) => number;

/** Grow `[from, to)` over every atomic range it overlaps, so a kill never splits a mention. */
function coverAtomicRanges(view: EditorView, from: number, to: number): [number, number] {
  let start = from;
  let end = to;
  for (const ranges of view.state.facet(EditorView.atomicRanges)) {
    ranges(view).between(from, to, (rangeFrom, rangeTo) => {
      if (rangeFrom < to && rangeTo > from) {
        start = Math.min(start, rangeFrom);
        end = Math.max(end, rangeTo);
      }
    });
  }
  return [start, end];
}

/** Kill from the caret to `boundary`, or kill the selection when there is one. */
function kill(boundary: Boundary): Command {
  return (view) => {
    const { state } = view;
    // A read-only or disabled draft keeps its text; the key falls through unclaimed.
    if (state.readOnly) return false;
    const range = state.selection.main;
    const text = state.doc.toString();
    const end = range.empty ? boundary(text, range.head) : range.to;
    const [from, to] = coverAtomicRanges(
      view,
      Math.min(range.empty ? range.head : range.from, end),
      Math.max(range.empty ? range.head : range.from, end),
    );
    if (from === to) return true;
    const backward = range.empty && to === range.head;
    view.dispatch(
      state.update({
        changes: { from, to },
        selection: { anchor: from },
        annotations: killed.of({ text: state.sliceDoc(from, to), backward }),
        scrollIntoView: true,
        userEvent: backward ? "delete.backward" : "delete.forward",
      }),
    );
    return true;
  };
}

/** ⌃K at the end of the line before a card does nothing: a kill never removes a card. */
const killToLineEnd: Command = (view) => {
  const { state } = view;
  const range = state.selection.main;
  const line = state.doc.lineAt(range.head);
  const next = line.number < state.doc.lines ? state.doc.line(line.number + 1) : null;
  if (!state.readOnly && range.empty && range.head === line.to && next !== null) {
    if (parseReplyQuoteLine(next.text) !== null) return true;
  }
  return kill(lineEndAfter)(view);
};

/** ⌃Y: insert the newest kill at the selection. */
const yank: StateCommand = ({ state, dispatch }) => {
  if (state.readOnly) return false;
  const text = yankText(state.field(killRingField));
  if (text === null) return true;
  const range = state.selection.main;
  dispatch(
    state.update({
      changes: { from: range.from, to: range.to, insert: text },
      selection: { anchor: range.from + text.length },
      scrollIntoView: true,
      userEvent: "input.paste",
    }),
  );
  return true;
};

/**
 * The kill-ring keys. The Emacs motion and delete keys are already bound on macOS by the
 * standard keymap; Ctrl-k overrides its line-end delete so the text can be yanked back.
 */
export const READLINE_BINDINGS: readonly KeyBinding[] = [
  { key: "Ctrl-k", run: killToLineEnd },
  { key: "Ctrl-u", run: kill(lineStartBefore) },
  { key: "Ctrl-w", run: kill(shellWordStartBefore) },
  { key: "Ctrl-y", run: yank },
];

/** The readline keys on macOS, and nothing elsewhere. */
export function readlineKeys(mac: boolean): Extension {
  return mac ? [killRingField, keymap.of(READLINE_BINDINGS)] : [];
}
