// What makes the CodeMirror viewer an editor: undo history, the editing
// commands and keys of other code editors, bracket closing and matching, ⌘S to
// save, and change and blur reports to the save logic.

import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import {
  history,
  historyKeymap,
  indentWithTab,
  redoSelection,
  undoSelection,
} from "@codemirror/commands";
import { bracketMatching } from "@codemirror/language";
import {
  Compartment,
  EditorState,
  type Extension,
  type Text,
  Transaction,
} from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";
import { rawDocumentOf, setRawDocument } from "./rawOffsets";
import { documentRefresh } from "./documentRefresh";
import { editorCommandKeymap } from "./codeMirrorEditorCommands";
import { languageEditing } from "./codeMirrorLanguageEditing";

export interface CodeMirrorEditingCallbacks {
  /** The document after an edit, or after new content loads. */
  onChange: (doc: Text) => void;
  /** ⌘S. */
  onSave: () => void;
  /** Focus left the editor, which flushes a pending save. */
  onBlur: () => void;
}

/** Drives an editable viewer from the save logic that owns its content. */
export interface CodeMirrorEditorHandle {
  /** Replaces the content with a newer copy of the file, keeping the reader's place. */
  loadContent: (raw: string) => void;
  /** Records the raw text just saved, so comments anchor to the file as it is now. */
  markSaved: (raw: string) => void;
}

const undoHistory = new Compartment();

/**
 * Read-only with the navigation and selection commands, or editable with the
 * editing keys too. `callbacks` is read on each event, so the caller can keep
 * it in a ref. `path` names the file, whose language sets comments and brackets.
 */
/**
 * Undo and redo for edits. CodeMirror's selection undo also binds ⌘U, and it
 * undoes edits too, so Undo cursor change owns that chord instead.
 */
const documentHistoryKeymap = historyKeymap.filter(
  (binding) => binding.run !== undoSelection && binding.run !== redoSelection,
);

export function editability(
  editable: boolean,
  callbacks: () => CodeMirrorEditingCallbacks | undefined,
  path: string,
): Extension {
  if (!editable) return [EditorState.readOnly.of(true), keymap.of(editorCommandKeymap(false))];
  return [
    undoHistory.of(history()),
    bracketMatching(),
    closeBrackets(),
    languageEditing(path),
    keymap.of([
      {
        key: "Mod-s",
        preventDefault: true,
        run: () => {
          callbacks()?.onSave();
          return true;
        },
      },
      ...editorCommandKeymap(true),
      ...closeBracketsKeymap,
      ...documentHistoryKeymap,
      indentWithTab,
    ]),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) callbacks()?.onChange(update.state.doc);
    }),
    EditorView.domEventHandlers({
      blur: () => {
        callbacks()?.onBlur();
        return false;
      },
    }),
  ];
}

/** A handle that loads newer file content into `view` and starts its undo history over. */
export function editorHandleFor(view: EditorView): CodeMirrorEditorHandle {
  return {
    loadContent: (raw) => {
      view.dispatch({
        ...documentRefresh(view, raw),
        annotations: Transaction.addToHistory.of(false),
      });
      // Undo shouldn't step back into the content the file had before it changed.
      view.dispatch({ effects: undoHistory.reconfigure([]) });
      view.dispatch({ effects: undoHistory.reconfigure(history()) });
    },
    markSaved: (raw) => view.dispatch({ effects: setRawDocument.of(rawDocumentOf(raw)) }),
  };
}

/**
 * Whether ⌘K offers the file's edit commands: runnable in an editable file,
 * greyed out with `readOnlyReason` when one is known, and otherwise hidden.
 */
export function editCommandAvailability(
  editable: boolean,
  readOnlyReason: string | undefined,
): CommandAvailability {
  if (editable) return AVAILABLE;
  return readOnlyReason ? unavailableBecause(readOnlyReason) : HIDDEN;
}
