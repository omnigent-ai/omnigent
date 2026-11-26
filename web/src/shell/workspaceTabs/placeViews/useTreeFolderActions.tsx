// The browsed folder's own actions in the Files tree: step up, back to the working folder,
// refresh, copy its path and open it elsewhere. The tree's ⋯ menu and the folder name's
// right-click menu list the same rows.

import { CornerLeftUpIcon, FolderDotIcon, RefreshCwIcon } from "lucide-react";
import type { ReactNode } from "react";

import { browseNavigation } from "@/shell/BrowseLocationBar";
import { fileCommandMenuIcon } from "@/shell/fileCommands/icons";
import { useFileCommands } from "@/shell/fileCommands/useFileCommands";
import { BACK_TO_WORKING_FOLDER_LABEL } from "@/shell/fileTree/treeRoot";
import { isCommandListed } from "@/store/paletteCommands";

/** One of the browsed folder's actions, such as Refresh or Open in VS Code. */
export interface TreeFolderAction {
  key: string;
  label: string;
  icon: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
}

export const GO_TO_PARENT_FOLDER_LABEL = "Go to parent folder";
export const REFRESH_FILES_LABEL = "Refresh files";
export const COPY_FOLDER_PATH_LABEL = "Copy folder path";

const FOLDER_COMMANDS = ["copy-path", "open-in-vscode", "open-in-cursor", "reveal"] as const;

export interface TreeFolderActionsInput {
  /** The session the tree shows, which may not be the one the app has focused. */
  conversationId: string;
  /** The absolute folder the tree shows, or null before the workspace is known. */
  folder: string | null;
  workspaceRoot: string | null;
  hostId: string | null;
  canBrowseOutside: boolean;
  reach: Parameters<typeof browseNavigation>[0]["reach"];
  /** Roots the tree at an absolute folder, or at the working folder for null. */
  onNavigate: (root: string | null) => void;
  refreshing: boolean;
  onRefresh: () => void;
}

export function useTreeFolderActions({
  conversationId,
  folder,
  workspaceRoot,
  hostId,
  canBrowseOutside,
  reach,
  onNavigate,
  refreshing,
  onRefresh,
}: TreeFolderActionsInput): TreeFolderAction[] {
  const folderCommands = useFileCommands(folder, { directory: true, conversationId });
  const actions: TreeFolderAction[] = [];
  if (folder && workspaceRoot) {
    const { parent, workspaceRoot: away } = browseNavigation({
      current: folder,
      workspace: workspaceRoot,
      hostId,
      canBrowseOutside,
      reach,
    });
    actions.push({
      key: "parent-folder",
      label: GO_TO_PARENT_FOLDER_LABEL,
      icon: <CornerLeftUpIcon />,
      disabled: parent === null,
      onSelect: () => {
        if (parent) onNavigate(parent === workspaceRoot ? null : parent);
      },
    });
    if (away) {
      actions.push({
        key: "working-folder",
        label: BACK_TO_WORKING_FOLDER_LABEL,
        icon: <FolderDotIcon />,
        onSelect: () => onNavigate(null),
      });
    }
  }
  actions.push({
    key: "refresh",
    label: REFRESH_FILES_LABEL,
    icon: <RefreshCwIcon />,
    disabled: refreshing,
    onSelect: onRefresh,
  });
  for (const id of FOLDER_COMMANDS) {
    const command = folderCommands.commands.find((candidate) => candidate.id === id);
    if (!command || !isCommandListed(command.availability)) continue;
    actions.push({
      key: id,
      label: id === "copy-path" ? COPY_FOLDER_PATH_LABEL : command.label,
      icon: fileCommandMenuIcon(id),
      disabled: !command.availability.available,
      onSelect: () => folderCommands.run(id),
    });
  }
  return actions;
}
