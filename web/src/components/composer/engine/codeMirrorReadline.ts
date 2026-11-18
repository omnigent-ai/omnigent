// The macOS readline keys for the CodeMirror composer, over the editor-free boundaries and
// kill ring in lib/textEditing/readline. Off macOS none of these keys are claimed: Ctrl+W,
// Ctrl+A and friends keep their platform meaning there.

import {
  cursorCharLeft,
  cursorCharRight,
  cursorLineDown,
  cursorLineEnd,
  cursorLineStart,
  cursorLineUp,
  deleteCharBackward,
  deleteCharForward,
  selectCharLeft,
  selectCharRight,
  selectLineDown,
  selectLineEnd,
  selectLineStart,
  selectLineUp,
  transposeChars,
} from "@codemirror/commands";
import { Annotation, StateField, type Extension, type StateCommand } from "@codemirror/state";
import { keymap, type KeyBinding } from "@codemirror/view";
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

const killed = Annotation.define<{ text: string; backward: boolean }>();

const killRingField = StateField.define<KillRing>({
  create: () => EMPTY_KILL_RING,
  update(ring, transaction) {
    const killedText = transaction.annotation(killed);
    if (killedText) return pushKill(ring, killedText.text, killedText.backward);
    return transaction.docChanged || transaction.selection ? endKillRun(ring) : ring;
  },
});

type Boundary = (text: string, offset: number) => number;

/** Kill from the caret to `boundary`, or kill the selection when there is one. */
function kill(boundary: Boundary): StateCommand {
  return ({ state, dispatch }) => {
    // A read-only or disabled draft keeps its text; the key falls through unclaimed.
    if (state.readOnly) return false;
    const range = state.selection.main;
    const text = state.doc.toString();
    const end = range.empty ? boundary(text, range.head) : range.to;
    const from = Math.min(range.empty ? range.head : range.from, end);
    const to = Math.max(range.empty ? range.head : range.from, end);
    if (from === to) return true;
    const backward = range.empty && to === range.head;
    dispatch(
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

/** The readline bindings, keyed in CodeMirror's notation. */
export const READLINE_BINDINGS: readonly KeyBinding[] = [
  { key: "Ctrl-a", run: cursorLineStart, shift: selectLineStart },
  { key: "Ctrl-e", run: cursorLineEnd, shift: selectLineEnd },
  { key: "Ctrl-b", run: cursorCharLeft, shift: selectCharLeft },
  { key: "Ctrl-f", run: cursorCharRight, shift: selectCharRight },
  { key: "Ctrl-p", run: cursorLineUp, shift: selectLineUp },
  { key: "Ctrl-n", run: cursorLineDown, shift: selectLineDown },
  { key: "Ctrl-d", run: deleteCharForward },
  { key: "Ctrl-h", run: deleteCharBackward },
  { key: "Ctrl-t", run: transposeChars },
  { key: "Ctrl-k", run: kill(lineEndAfter) },
  { key: "Ctrl-u", run: kill(lineStartBefore) },
  { key: "Ctrl-w", run: kill(shellWordStartBefore) },
  { key: "Ctrl-y", run: yank },
];

/** The readline keys on macOS, and nothing elsewhere. */
export function readlineKeys(mac: boolean): Extension {
  return mac ? [killRingField, keymap.of(READLINE_BINDINGS)] : [];
}
