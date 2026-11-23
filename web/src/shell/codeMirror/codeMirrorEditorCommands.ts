// CodeMirror implementations of the file editor's commands, keyed by their
// shortcut registry ids, and the editor keymap built from the registry's chords.

import {
  addCursorAbove,
  addCursorBelow,
  copyLineDown,
  copyLineUp,
  cursorMatchingBracket,
  deleteLine,
  deleteTrailingWhitespace,
  indentLess,
  indentMore,
  insertBlankLine,
  moveLineDown,
  moveLineUp,
  simplifySelection,
  standardKeymap,
  toggleTabFocusMode,
  toggleBlockComment,
  toggleComment,
} from "@codemirror/commands";
import { foldAll, foldCode, unfoldAll, unfoldCode } from "@codemirror/language";
import { selectNextOccurrence, selectSelectionMatches } from "@codemirror/search";
import { Annotation, EditorSelection, StateField } from "@codemirror/state";
import type { Command, KeyBinding } from "@codemirror/view";
import { FILE_EDITOR_COMMANDS, type FileEditorCommandId } from "@/components/fileEditorCommands";
import { codeMirrorKeys, matchesShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

/** Opens an empty line above each cursor's line, indented like it. */
export const insertLineAbove: Command = (view) => {
  if (view.state.readOnly) return false;
  const changes = view.state.changeByRange((range) => {
    const line = view.state.doc.lineAt(range.head);
    const indentation = /^\s*/.exec(line.text)?.[0] ?? "";
    return {
      changes: { from: line.from, insert: `${indentation}${view.state.lineBreak}` },
      range: EditorSelection.cursor(line.from + indentation.length),
    };
  });
  view.dispatch(view.state.update(changes, { scrollIntoView: true, userEvent: "input" }));
  return true;
};

const CURSOR_HISTORY_LIMIT = 100;
const cursorUndoStep = Annotation.define<true>();

/**
 * Earlier selections for Undo cursor change, newest last. Any document change
 * clears them, so stepping back through cursors can never edit the file.
 */
export const cursorHistory = StateField.define<readonly EditorSelection[]>({
  create: () => [],
  update(previous, transaction) {
    if (transaction.docChanged) return [];
    if (transaction.annotation(cursorUndoStep)) return previous.slice(0, -1);
    if (!transaction.selection || transaction.selection.eq(transaction.startState.selection)) {
      return previous;
    }
    return [...previous, transaction.startState.selection].slice(-CURSOR_HISTORY_LIMIT);
  },
});

/** Restores the previous selection, and only the selection; false when there is none. */
export const undoCursorChange: Command = (view) => {
  const previous = view.state.field(cursorHistory, false)?.at(-1);
  if (!previous) return false;
  view.dispatch({
    selection: previous,
    annotations: cursorUndoStep.of(true),
    scrollIntoView: true,
    userEvent: "select",
  });
  return true;
};

/**
 * Selects every match of the main selection, as other code editors do: from a
 * bare caret it takes the word under it, and of several selections it keeps the main one.
 */
export const selectAllOccurrences: Command = (view) => {
  const { state } = view;
  const main = state.selection.main;
  const seed = main.empty ? state.wordAt(main.head) : main;
  if (!seed) return false;
  if (state.selection.ranges.length > 1 || main.empty) {
    view.dispatch({ selection: EditorSelection.single(seed.from, seed.to), userEvent: "select" });
  }
  return selectSelectionMatches(view);
};

/** Selects each cursor's whole line, and on repeat adds the next line, stopping at the end. */
export const expandLineSelection: Command = (view) => {
  const { doc } = view.state;
  const ranges = view.state.selection.ranges.map((range) => {
    const from = doc.lineAt(range.from).from;
    const to = Math.min(doc.lineAt(range.to).to + 1, doc.length);
    return EditorSelection.range(from, to);
  });
  view.dispatch({
    selection: EditorSelection.create(ranges, view.state.selection.mainIndex),
    scrollIntoView: true,
    userEvent: "select",
  });
  return true;
};

/** What each file editor command does in CodeMirror. */
export const EDITOR_COMMAND_RUNS: Readonly<Record<FileEditorCommandId, Command>> = {
  "file.toggleLineComment": toggleComment,
  "file.toggleBlockComment": toggleBlockComment,
  "file.indentLines": indentMore,
  "file.outdentLines": indentLess,
  "file.moveLinesUp": moveLineUp,
  "file.moveLinesDown": moveLineDown,
  "file.copyLinesUp": copyLineUp,
  "file.copyLinesDown": copyLineDown,
  "file.deleteLines": deleteLine,
  "file.insertLineBelow": insertBlankLine,
  "file.insertLineAbove": insertLineAbove,
  "file.trimTrailingWhitespace": deleteTrailingWhitespace,
  "file.undoCursor": undoCursorChange,
  "file.addCursorAbove": addCursorAbove,
  "file.addCursorBelow": addCursorBelow,
  "file.selectNextOccurrence": selectNextOccurrence,
  "file.selectAllOccurrences": selectAllOccurrences,
  "file.expandLineSelection": expandLineSelection,
  "file.goToBracket": cursorMatchingBracket,
  "file.fold": foldCode,
  "file.unfold": unfoldCode,
  "file.foldAll": foldAll,
  "file.unfoldAll": unfoldAll,
};

/**
 * Cursor movement, selection, Enter (keeping the line's indentation), Backspace
 * and Delete on every cursor, Escape back to one cursor, and the Tab focus mode
 * toggle that lets keyboard users leave the editor with Tab.
 */
export const baseEditorKeymap: readonly KeyBinding[] = [
  ...standardKeymap,
  { key: "Escape", run: simplifySelection },
  { key: "Ctrl-m", mac: "Shift-Alt-m", run: toggleTabFocusMode },
];

/** Key bindings for the commands that apply to an editable or read-only file. */
export function editorCommandKeymap(editable: boolean): KeyBinding[] {
  const commands = FILE_EDITOR_COMMANDS.filter((command) => editable || !command.edits);
  return [
    ...commands.flatMap((command) =>
      codeMirrorKeys(command.id).map((key) => ({
        key,
        run: EDITOR_COMMAND_RUNS[command.id],
        preventDefault: true,
      })),
    ),
    {
      // On macOS ⌥ turns a letter into another character (⇧⌥A types Å), which
      // CodeMirror's key names don't match, so match those chords by physical key.
      any: (view, event) => {
        if (!event.altKey || event.isComposing) return false;
        const command = commands.find((candidate) => matchesShortcut(event, candidate.id));
        return command ? EDITOR_COMMAND_RUNS[command.id](view) : false;
      },
    },
  ];
}
