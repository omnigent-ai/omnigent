// What the file editor's right-click menu lists, from the same labels and
// shortcuts as ⌘K, given what the editor can do at the click.

import type { EditorState } from "@codemirror/state";
import type { FileEditorCommandId } from "@/components/fileEditorCommands";
import { GO_TO_LINE_LABEL } from "@/components/GoToLineBar";
import { searchQueryFromSelection } from "@/lib/selectedText";
import { shortcutBinding } from "@/lib/shortcuts/registry";
import type { ShortcutId } from "@/lib/shortcuts/registry";
import { WORKSPACE_TEXT_SEARCH_COPY } from "@/lib/workspaceTextSearchCopy";
import { ADD_COMMENT, ATTACH_TO_AGENT } from "../CommentSelectionActions";

export const FIND_IN_FILE_LABEL = "Find in file";
export const FIND_AND_REPLACE_LABEL = "Find and replace in file";

export type EditorMenuAction =
  | "search-workspace"
  | "add-comment"
  | "attach-to-agent"
  | "cut"
  | "copy"
  | "paste"
  | "find"
  | "find-and-replace"
  | "go-to-line"
  | FileEditorCommandId;

export type EditorMenuEntry =
  | { kind: "separator"; key: string }
  | {
      kind: "item";
      action: EditorMenuAction;
      label: string;
      shortcut?: ShortcutId;
      /** Keys shown for clipboard rows, which the shortcut registry doesn't own. */
      keys?: readonly string[];
      enabled: boolean;
      description?: string;
    };

/** What the editor can do where the menu opened. */
export interface EditorMenuContext {
  editable: boolean;
  /** The main selection has text. */
  hasSelection: boolean;
  /** Some range of the selection has text, as a column whose main range is a cursor can. */
  hasSelectedText: boolean;
  /** The selection makes a workspace search query and the finder serves this session. */
  canSearchWorkspace: boolean;
  canComment: boolean;
  canAttachToAgent: boolean;
  /** The page may read the clipboard; the desktop app leaves Paste to ⌘V. */
  canReadClipboard: boolean;
  mac: boolean;
}

function clipboardKeys(letter: string, mac: boolean): readonly string[] {
  return mac ? ["⌘", letter] : ["Ctrl", letter];
}

/** Why Paste is disabled where the page can't read the clipboard, naming the row's own keys. */
export function pasteWithKeysDescription(mac: boolean): string {
  const keys = clipboardKeys("V", mac).join(mac ? "" : "+");
  return `Press ${keys} to paste here.`;
}

function editorCommand(id: FileEditorCommandId): EditorMenuEntry {
  return {
    kind: "item",
    action: id,
    label: shortcutBinding(id).label,
    shortcut: id,
    enabled: true,
  };
}

/** The menu's rows in order, with separators only between non-empty groups. */
export function editorMenuEntries(context: EditorMenuContext): EditorMenuEntry[] {
  const { editable, hasSelection, mac } = context;
  const selection: EditorMenuEntry[] = [];
  if (context.canSearchWorkspace) {
    selection.push({
      kind: "item",
      action: "search-workspace",
      label: WORKSPACE_TEXT_SEARCH_COPY.command,
      shortcut: "palette.text",
      enabled: true,
    });
  }
  if (hasSelection && context.canComment) {
    selection.push({
      kind: "item",
      action: "add-comment",
      label: ADD_COMMENT.label,
      enabled: true,
    });
    if (context.canAttachToAgent) {
      selection.push({
        kind: "item",
        action: "attach-to-agent",
        label: ATTACH_TO_AGENT.label,
        enabled: true,
      });
    }
  }
  const clipboard: EditorMenuEntry[] = [];
  if (editable) {
    clipboard.push({
      kind: "item",
      action: "cut",
      label: "Cut",
      keys: clipboardKeys("X", mac),
      enabled: context.hasSelectedText,
    });
  }
  clipboard.push({
    kind: "item",
    action: "copy",
    label: "Copy",
    keys: clipboardKeys("C", mac),
    enabled: context.hasSelectedText,
  });
  if (editable) {
    clipboard.push({
      kind: "item",
      action: "paste",
      label: "Paste",
      keys: clipboardKeys("V", mac),
      enabled: context.canReadClipboard,
      description: context.canReadClipboard ? undefined : pasteWithKeysDescription(mac),
    });
  }
  const navigation: EditorMenuEntry[] = [
    {
      kind: "item",
      action: "find",
      label: FIND_IN_FILE_LABEL,
      shortcut: "find.open",
      enabled: true,
    },
  ];
  if (editable) {
    navigation.push({
      kind: "item",
      action: "find-and-replace",
      label: FIND_AND_REPLACE_LABEL,
      shortcut: "file.findAndReplace",
      enabled: true,
    });
  }
  navigation.push({
    kind: "item",
    action: "go-to-line",
    label: `${GO_TO_LINE_LABEL}…`,
    shortcut: "file.goToLine",
    enabled: true,
  });
  const editing: EditorMenuEntry[] = [editorCommand("file.selectAllOccurrences")];
  if (editable) editing.push(editorCommand("file.toggleLineComment"));
  editing.push(editorCommand("file.foldAll"), editorCommand("file.unfoldAll"));

  const groups = [selection, clipboard, navigation, editing].filter((group) => group.length > 0);
  return groups.flatMap((group, index) =>
    index === 0 ? group : [{ kind: "separator", key: `separator-${index}` } as const, ...group],
  );
}

/**
 * The main selection's search query, reading only up to its first non-blank line, so a
 * menu opened over a select-all doesn't copy the whole file.
 */
export function selectionSearchQuery(state: EditorState): string | null {
  const { from, to, empty } = state.selection.main;
  if (empty) return null;
  for (let number = state.doc.lineAt(from).number; ; number += 1) {
    const line = state.doc.line(number);
    const text = line.text.slice(
      Math.max(from, line.from) - line.from,
      Math.min(to, line.to) - line.from,
    );
    if (/\S/.test(text) || line.to >= to) return searchQueryFromSelection(text);
  }
}
