// The Markdown textarea's keys: the formatting chords and Write/Preview toggle, ⌘↵ and Esc, and
// the shell-style Control keys on macOS. Every edit is one native undo step.

import { chordMatches, type Chord } from "@/lib/shortcuts/chord";
import { lineEndAfter, lineStartBefore, shellWordStartBefore } from "@/lib/textEditing/readline";

import { type SharedKillRing, sharedKillRing } from "./killRing";
import {
  applyUndoableTextEdit,
  formatMarkdownSelection,
  insertText,
  type MarkdownFormat,
  markdownEditorShortcut,
  textareaEdit,
} from "./markdownFormatting";

/** Apply `format` to the textarea's selection. */
export function applyMarkdownFormat(textarea: HTMLTextAreaElement, format: MarkdownFormat): void {
  applyUndoableTextEdit(textarea, formatMarkdownSelection(textareaEdit(textarea), format));
}

function kill(
  textarea: HTMLTextAreaElement,
  killRing: SharedKillRing,
  range: (text: string, offset: number) => number,
  direction: "backward" | "forward",
  continuing: boolean,
): void {
  const { value, selectionStart, selectionEnd } = textarea;
  if (selectionStart !== selectionEnd) return;
  const stop = range(value, selectionStart);
  const [from, to] = direction === "backward" ? [stop, selectionStart] : [selectionStart, stop];
  if (from === to) return;
  if (!continuing) killRing.breakSequence();
  killRing.push(value.slice(from, to), direction);
  applyUndoableTextEdit(textarea, {
    value: value.slice(0, from) + value.slice(to),
    selectionStart: from,
    selectionEnd: from,
  });
}

export interface MarkdownTextareaKeyOptions {
  mac: boolean;
  onSubmit: () => void;
  onCancel?: () => void;
  onTogglePreview?: () => void;
  killRing?: SharedKillRing;
}

const control = (letter: string): Chord => ({ ctrl: true, code: `Key${letter}` });

// The value right after this textarea's own last kill, so the next kill joins it.
const lastKillValues = new WeakMap<HTMLTextAreaElement, { value: string; caret: number }>();

/** Handle a keydown in the textarea; true when the key was the editor's and is now consumed. */
export function handleMarkdownTextareaKey(
  event: KeyboardEvent,
  textarea: HTMLTextAreaElement,
  {
    mac,
    onSubmit,
    onCancel,
    onTogglePreview,
    killRing = sharedKillRing,
  }: MarkdownTextareaKeyOptions,
): boolean {
  if (event.isComposing) return false;
  const matches = (chord: Chord) => chordMatches(chord, event, mac);
  if (matches({ mod: true, key: "Enter" })) {
    onSubmit();
    return true;
  }
  if (onCancel && matches({ key: "Escape" })) {
    onCancel();
    return true;
  }
  const shortcut = markdownEditorShortcut(event, mac);
  if (shortcut === "toggle-preview") {
    if (!onTogglePreview) return false;
    onTogglePreview();
    return true;
  }
  if (shortcut) {
    applyMarkdownFormat(textarea, shortcut);
    return true;
  }
  if (!mac) return false;
  const last = lastKillValues.get(textarea);
  // A kill joins the last one only where it left the text and caret, not after a move.
  const continuing = last?.value === textarea.value && last.caret === textarea.selectionStart;
  const kills: [string, (text: string, offset: number) => number, "backward" | "forward"][] = [
    ["W", shellWordStartBefore, "backward"],
    ["U", lineStartBefore, "backward"],
    ["K", lineEndAfter, "forward"],
  ];
  for (const [letter, range, direction] of kills) {
    if (!matches(control(letter))) continue;
    kill(textarea, killRing, range, direction, continuing);
    lastKillValues.set(textarea, { value: textarea.value, caret: textarea.selectionStart });
    return true;
  }
  if (matches(control("Y"))) {
    const text = killRing.yank();
    if (text) applyUndoableTextEdit(textarea, insertText(textareaEdit(textarea), text));
    return true;
  }
  return false;
}
