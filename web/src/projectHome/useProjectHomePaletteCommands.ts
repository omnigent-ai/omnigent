// With quick_pick_palette on, ⌘K offers the open project's header actions: a new session in
// it, its settings, and Rename and Delete from its menu, each through the header's handler.

import {
  MessageCirclePlusIcon,
  PencilIcon,
  Settings2Icon,
  Trash2Icon,
  type LucideIcon,
} from "lucide-react";
import { useMemo } from "react";

import type { ProjectFolderMenuActions } from "@/shell/Sidebar";
import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

interface ProjectHomeCommand {
  id: string;
  label: string;
  icon: LucideIcon;
  destructive?: boolean;
  run: () => void;
}

/**
 * :param startSession: Opens the project's composer, as its New session button does.
 */
export function useProjectHomePaletteCommands(
  projectName: string,
  actions: Pick<ProjectFolderMenuActions, "openSettings" | "openRename" | "openDelete">,
  startSession: () => void,
): void {
  const commands = useMemo<PaletteCommand[]>(() => {
    const entries: ProjectHomeCommand[] = [
      {
        id: "new-session",
        label: "New session in this project",
        icon: MessageCirclePlusIcon,
        run: startSession,
      },
      {
        id: "settings",
        label: "Project settings…",
        icon: Settings2Icon,
        run: actions.openSettings,
      },
      { id: "rename", label: "Rename project…", icon: PencilIcon, run: actions.openRename },
      {
        id: "delete",
        label: "Delete project…",
        icon: Trash2Icon,
        destructive: true,
        run: actions.openDelete,
      },
    ];
    // Each opens a composer or dialog, so it runs once the palette has closed.
    return entries.map(({ id, ...command }) => ({
      ...command,
      id: `project.${id}:${projectName}`,
      keywords: ["project", "folder", projectName],
      movesFocus: true,
    }));
  }, [projectName, actions, startSession]);
  useQuickPickPaletteCommands(`project-home:${projectName}`, "Project", commands);
}
