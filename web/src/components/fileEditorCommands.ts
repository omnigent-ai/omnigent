// The file editor's commands as the app lists them in the command palette:
// icons, search terms, and whether a command edits. Labels and chords live in
// the shortcut registry under the same ids, and the CodeMirror implementations
// in shell/codeMirror/codeMirrorEditorCommands.ts, so they can't drift.

import {
  ArrowDownIcon,
  ArrowUpIcon,
  BetweenHorizontalEndIcon,
  BetweenHorizontalStartIcon,
  BracketsIcon,
  ChevronsDownUpIcon,
  ChevronsUpDownIcon,
  CopyIcon,
  EraserIcon,
  FoldVerticalIcon,
  IndentDecreaseIcon,
  IndentIncreaseIcon,
  MessageSquareCodeIcon,
  TextCursorInputIcon,
  TextSelectIcon,
  Trash2Icon,
  Undo2Icon,
  UnfoldVerticalIcon,
  WholeWordIcon,
  type LucideIcon,
} from "lucide-react";
import { shortcutBinding } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

/** A shortcut registry id that names a file editor command. */
export type FileEditorCommandId =
  | "file.toggleLineComment"
  | "file.toggleBlockComment"
  | "file.indentLines"
  | "file.outdentLines"
  | "file.moveLinesUp"
  | "file.moveLinesDown"
  | "file.copyLinesUp"
  | "file.copyLinesDown"
  | "file.deleteLines"
  | "file.insertLineBelow"
  | "file.insertLineAbove"
  | "file.trimTrailingWhitespace"
  | "file.undoCursor"
  | "file.addCursorAbove"
  | "file.addCursorBelow"
  | "file.selectNextOccurrence"
  | "file.selectAllOccurrences"
  | "file.expandLineSelection"
  | "file.goToBracket"
  | "file.fold"
  | "file.unfold"
  | "file.foldAll"
  | "file.unfoldAll";

export interface FileEditorCommand {
  id: FileEditorCommandId;
  icon: LucideIcon;
  /** Extra terms the palette filter matches. */
  keywords: string[];
  /** Whether the command changes the document, so needs an editable file. */
  edits: boolean;
}

export const FILE_EDITOR_COMMANDS: readonly FileEditorCommand[] = [
  {
    id: "file.toggleLineComment",
    icon: MessageSquareCodeIcon,
    keywords: ["comment", "uncomment", "line comment"],
    edits: true,
  },
  {
    id: "file.toggleBlockComment",
    icon: MessageSquareCodeIcon,
    keywords: ["comment", "block comment"],
    edits: true,
  },
  {
    id: "file.indentLines",
    icon: IndentIncreaseIcon,
    keywords: ["indent", "tab", "shift right"],
    edits: true,
  },
  {
    id: "file.outdentLines",
    icon: IndentDecreaseIcon,
    keywords: ["outdent", "dedent", "unindent", "shift left"],
    edits: true,
  },
  {
    id: "file.moveLinesUp",
    icon: ArrowUpIcon,
    keywords: ["move line", "swap"],
    edits: true,
  },
  {
    id: "file.moveLinesDown",
    icon: ArrowDownIcon,
    keywords: ["move line", "swap"],
    edits: true,
  },
  {
    id: "file.copyLinesUp",
    icon: CopyIcon,
    keywords: ["duplicate line", "copy line"],
    edits: true,
  },
  {
    id: "file.copyLinesDown",
    icon: CopyIcon,
    keywords: ["duplicate line", "copy line"],
    edits: true,
  },
  {
    id: "file.deleteLines",
    icon: Trash2Icon,
    keywords: ["remove line", "delete line"],
    edits: true,
  },
  {
    id: "file.insertLineBelow",
    icon: BetweenHorizontalEndIcon,
    keywords: ["new line", "blank line"],
    edits: true,
  },
  {
    id: "file.insertLineAbove",
    icon: BetweenHorizontalStartIcon,
    keywords: ["new line", "blank line"],
    edits: true,
  },
  {
    id: "file.trimTrailingWhitespace",
    icon: EraserIcon,
    keywords: ["whitespace", "trailing spaces", "clean"],
    edits: true,
  },
  {
    id: "file.undoCursor",
    icon: Undo2Icon,
    keywords: ["cursor undo", "selection undo"],
    edits: false,
  },
  {
    id: "file.addCursorAbove",
    icon: TextCursorInputIcon,
    keywords: ["multiple cursors", "multi-cursor", "caret"],
    edits: false,
  },
  {
    id: "file.addCursorBelow",
    icon: TextCursorInputIcon,
    keywords: ["multiple cursors", "multi-cursor", "caret"],
    edits: false,
  },
  {
    id: "file.selectNextOccurrence",
    icon: WholeWordIcon,
    keywords: ["select next", "multiple cursors", "match"],
    edits: false,
  },
  {
    id: "file.selectAllOccurrences",
    icon: WholeWordIcon,
    keywords: ["select all matches", "multiple cursors"],
    edits: false,
  },
  {
    id: "file.expandLineSelection",
    icon: TextSelectIcon,
    keywords: ["select line"],
    edits: false,
  },
  {
    id: "file.goToBracket",
    icon: BracketsIcon,
    keywords: ["matching bracket", "jump", "parenthesis"],
    edits: false,
  },
  {
    id: "file.fold",
    icon: FoldVerticalIcon,
    keywords: ["collapse", "fold region"],
    edits: false,
  },
  {
    id: "file.unfold",
    icon: UnfoldVerticalIcon,
    keywords: ["expand", "unfold region"],
    edits: false,
  },
  {
    id: "file.foldAll",
    icon: ChevronsDownUpIcon,
    keywords: ["collapse all"],
    edits: false,
  },
  {
    id: "file.unfoldAll",
    icon: ChevronsUpDownIcon,
    keywords: ["expand all"],
    edits: false,
  },
];

/** The command's title, shared with the Keyboard shortcuts page. */
export function fileEditorCommandLabel(command: FileEditorCommand): string {
  return shortcutBinding(command.id).label;
}
