// The macOS readline keys for the CodeMirror composer, over the editor-free boundaries and
// kill ring in lib/textEditing/readline. Off macOS none of these keys are claimed: Ctrl+W,
// Ctrl+A and friends keep their platform meaning there.

import {
  Annotation,
  Facet,
  StateField,
  type EditorState,
  type Extension,
  type StateCommand,
} from "@codemirror/state";
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

/** Spans no kill reaches into, such as a reply card with the line breaks beside it. */
export const killStops =
  Facet.define<(state: EditorState) => readonly { from: number; to: number }[]>();

/** `[from, to)` cut short at the stops on either side of `origin`; empty when `origin` is in one. */
function stopKill(state: EditorState, from: number, to: number, origin: number): [number, number] {
  let [start, end] = [from, to];
  for (const stops of state.facet(killStops)) {
    for (const stop of stops(state)) {
      if (stop.to <= start || stop.from >= end) continue;
      if (stop.to <= origin) start = Math.max(start, stop.to);
      else if (stop.from >= origin) end = Math.min(end, stop.from);
      else return [origin, origin];
    }
  }
  return [start, end];
}

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
    const covered = coverAtomicRanges(
      view,
      Math.min(range.empty ? range.head : range.from, end),
      Math.max(range.empty ? range.head : range.from, end),
    );
    // A selection's kill takes all of it, a card included; only a caret's kill stops at one.
    const [from, to] = range.empty ? stopKill(state, ...covered, range.head) : covered;
    if (from === to) return true;
    const backward = range.empty && to === range.head;
    // The ring keeps what a copy would, so a yank brings back a reply card as its `> ` quote.
    const copied = state
      .facet(EditorView.clipboardOutputFilter)
      .reduce((slice, filter) => filter(slice, state), state.sliceDoc(from, to));
    view.dispatch(
      state.update({
        changes: { from, to },
        selection: { anchor: from },
        annotations: killed.of({ text: copied, backward }),
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

/**
 * The kill-ring keys. The Emacs motion and delete keys are already bound on macOS by the
 * standard keymap; Ctrl-k overrides its line-end delete so the text can be yanked back.
 */
export const READLINE_BINDINGS: readonly KeyBinding[] = [
  { key: "Ctrl-k", run: kill(lineEndAfter) },
  { key: "Ctrl-u", run: kill(lineStartBefore) },
  { key: "Ctrl-w", run: kill(shellWordStartBefore) },
  { key: "Ctrl-y", run: yank },
];

/** The readline keys on macOS, and nothing elsewhere. */
export function readlineKeys(mac: boolean): Extension {
  return mac ? [killRingField, keymap.of(READLINE_BINDINGS)] : [];
}
