// The actions every surface offers on a workspace file or folder: the file
// viewer's toolbar, a diff review file's header menu, the Files tree's context
// menu, a chat file link's context menu, and ⌘K. This module decides each
// action's label and availability from plain facts about one target, so the
// surfaces can't drift. It holds no React; icons and implementations live beside
// the surfaces and in the action host.

import { CODE_EDITORS, type CodeEditor } from "@/lib/editorLinks";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";
import type { CodeHostFileLink } from "./codeHostFileLink";

/** Every file action, in the order a full menu lists them. */
export const FILE_COMMAND_IDS = [
  "open",
  "open-in-new-tab",
  "open-as-root",
  "open-on-code-host",
  "copy-path",
  "copy-relative-path",
  "copy-previous-path",
  "copy-link",
  "reveal",
  "open-in-vscode",
  "open-in-cursor",
] as const;

export type FileCommandId = (typeof FILE_COMMAND_IDS)[number];

/** Opens a file in its own workspace tab, in the Files and Changes trees' menus. */
export const OPEN_IN_NEW_TAB_LABEL = "Open in new tab";

/** Re-roots the Files tree at a folder, as double-clicking the folder does. */
export const OPEN_FOLDER_AS_ROOT_LABEL = "Open folder as root";

/** The copy actions' labels, shared by the menus and the copy-path buttons. */
export const COPY_PATH_LABELS = {
  "copy-path": "Copy path",
  "copy-relative-path": "Copy relative path",
} as const;

/** A renamed file's path before the rename, in a diff review's menu and ⌘K. */
export const COPY_PREVIOUS_PATH_LABEL = "Copy previous path";

/** ⌘K's names for the viewed file's actions, matching VS Code's commands. */
export const ACTIVE_FILE_PALETTE_LABELS: Partial<Record<FileCommandId, string>> = {
  "copy-path": "Copy Path of Active File",
  "copy-relative-path": "Copy Relative Path of Active File",
};

/** The "Open in" action for each code editor. */
export const OPEN_IN_EDITOR: Record<CodeEditor, FileCommandId> = {
  vscode: "open-in-vscode",
  cursor: "open-in-cursor",
};

/** What the actions need to know about one file or folder and where it lives. */
export interface FileCommandFacts {
  directory: boolean;
  /** Deleted in the working copy, so there's nothing to open, reveal, or edit. */
  deleted: boolean;
  /** Its path from the workspace root; `null` when it's outside the workspace. */
  relativePath: string | null;
  /** Its absolute path on the session's host; `null` until the workspace root is known. */
  absolutePath: string | null;
  /** The surface can open it in the viewer, and isn't already showing it. */
  openable: boolean;
  /** The surface can open it in a workspace tab of its own. */
  newTabOpenable: boolean;
  /** The surface browses folders and can make this one its root. */
  rootable: boolean;
  /** A session is open, so a link can point at the file in it. */
  linkable: boolean;
  /** It's on this machine and the desktop app can show it in the file manager. */
  revealable: boolean;
  /** This OS's file manager, such as "Finder". */
  fileManager: string;
  /** The editors that can open it here, locally or over SSH. */
  editors: readonly CodeEditor[];
  /** Where a pull request review opens it on its code host; absent elsewhere. */
  codeHostLink?: CodeHostFileLink | null;
  /** A renamed file's path before the rename, in a diff review; absent elsewhere. */
  previousPath?: string | null;
}

export interface FileCommand {
  id: FileCommandId;
  label: string;
  /** Extra terms ⌘K matches. */
  keywords: readonly string[];
  availability: CommandAvailability;
}

function when(condition: boolean, availability: CommandAvailability = AVAILABLE) {
  return condition ? availability : HIDDEN;
}

/** Every file action for `facts`, in full-menu order. */
export function fileCommands(facts: FileCommandFacts): FileCommand[] {
  const present = !facts.deleted;
  const noun = facts.directory ? "folder" : "file";
  const commands: FileCommand[] = [
    {
      id: "open",
      label: "Open file",
      keywords: ["view", "show", "viewer"],
      availability: when(facts.openable && present && !facts.directory),
    },
    {
      id: "open-in-new-tab",
      label: OPEN_IN_NEW_TAB_LABEL,
      keywords: ["tab", "new", "background"],
      availability: when(facts.newTabOpenable && present && !facts.directory),
    },
    {
      id: "open-as-root",
      label: OPEN_FOLDER_AS_ROOT_LABEL,
      keywords: ["root", "browse", "folder", "navigate"],
      availability: when(facts.rootable && present && facts.directory),
    },
    {
      id: "open-on-code-host",
      label: facts.codeHostLink?.label ?? "Open in code host",
      keywords: ["github", "browser", "remote", "web"],
      availability: when(
        !!facts.codeHostLink,
        facts.codeHostLink?.url === null
          ? unavailableBecause(facts.codeHostLink.unavailableReason)
          : AVAILABLE,
      ),
    },
    {
      id: "copy-path",
      label: COPY_PATH_LABELS["copy-path"],
      keywords: ["clipboard", "absolute", "full path", noun],
      // Until the workspace location loads, the path from the workspace root is all there is.
      availability: when(facts.absolutePath !== null || facts.relativePath !== null),
    },
    {
      id: "copy-relative-path",
      label: COPY_PATH_LABELS["copy-relative-path"],
      keywords: ["clipboard", "workspace", noun],
      // Without a full path, "Copy path" already copies this one.
      availability: when(
        facts.absolutePath !== null && facts.relativePath !== null && facts.relativePath !== "",
      ),
    },
    {
      id: "copy-previous-path",
      label: COPY_PREVIOUS_PATH_LABEL,
      keywords: ["clipboard", "renamed", "old", "before", "rename"],
      availability: when(!!facts.previousPath),
    },
    {
      id: "copy-link",
      label: "Copy link to file",
      keywords: ["url", "clipboard", "share"],
      // A link opens the file in the session's viewer, which shows files, not folders.
      availability: when(facts.linkable && facts.relativePath !== null && !facts.directory),
    },
    {
      id: "reveal",
      label: `${facts.directory ? "Open" : "Show"} in ${facts.fileManager}`,
      keywords: ["finder", "explorer", "file manager", "reveal", "folder"],
      availability: when(facts.revealable && present),
    },
  ];
  for (const editor of CODE_EDITORS) {
    commands.push({
      id: OPEN_IN_EDITOR[editor.id],
      label: `Open in ${editor.label}`,
      keywords: ["editor", "ide", "code", editor.label],
      availability: when(present && facts.editors.includes(editor.id)),
    });
  }
  return commands;
}

/** `fileCommands(facts)` keyed by id, for a surface listing them in its own order. */
export function fileCommandsById(facts: FileCommandFacts): Record<FileCommandId, FileCommand> {
  return Object.fromEntries(fileCommands(facts).map((entry) => [entry.id, entry])) as Record<
    FileCommandId,
    FileCommand
  >;
}
