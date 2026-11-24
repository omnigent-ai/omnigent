// The workspace's actions in ⌘K, from the same commands as its "+" menu and panel picker.

import {
  ArrowLeftIcon,
  ArrowRightIcon,
  BotIcon,
  FileDiffIcon,
  FolderTreeIcon,
  GitMergeIcon,
  GitPullRequestIcon,
  GlobeIcon,
  ListChecksIcon,
  MaximizeIcon,
  MessagesSquareIcon,
  MinimizeIcon,
  PanelRightIcon,
  PinIcon,
  PlusIcon,
  RotateCcwIcon,
  SearchIcon,
  TerminalIcon,
  type LucideIcon,
  XIcon,
} from "lucide-react";
import { useMemo, useRef } from "react";

import type { DefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import type { WorkspaceCommand, WorkspaceTabAction } from "./model";

const PANEL_ICONS: Record<DefaultWorkspaceTab, LucideIcon> = {
  files: FolderTreeIcon,
  changes: FileDiffIcon,
  github: GitPullRequestIcon,
  subagents: BotIcon,
  tasks: ListChecksIcon,
};

const TAB_ACTION_ICONS: Record<WorkspaceTabAction, LucideIcon> = {
  "new-tab": PlusIcon,
  "close-tab": XIcon,
  reopen: RotateCcwIcon,
  "close-others": XIcon,
  "move-left": ArrowLeftIcon,
  "move-right": ArrowRightIcon,
  "toggle-pin": PinIcon,
  "search-tabs": SearchIcon,
  "close-merged-pull-requests": GitMergeIcon,
  "close-pull-request-tabs": XIcon,
  "close-file-tabs": XIcon,
  "close-page-tabs": XIcon,
};

/** The icon a workspace command shows in ⌘K. */
export function workspaceCommandIcon(command: WorkspaceCommand): LucideIcon {
  switch (command.kind) {
    case "panel":
      return PANEL_ICONS[command.tab];
    case "browser":
      return GlobeIcon;
    case "side-chat":
      return MessagesSquareIcon;
    case "shell":
      return TerminalIcon;
    case "show":
      return PanelRightIcon;
    case "close":
      return XIcon;
    case "full-screen":
      return command.fullScreen ? MinimizeIcon : MaximizeIcon;
    case "history":
      return command.direction === "back" ? ArrowLeftIcon : ArrowRightIcon;
    case "tab-action":
      return TAB_ACTION_ICONS[command.action];
  }
}

/** Offer a workspace's commands under "Workspace"; pass none while it isn't on screen. */
export function useWorkspacePaletteCommands({
  conversationId,
  commands,
  onRun,
}: {
  conversationId: string;
  commands: readonly WorkspaceCommand[];
  onRun: (command: WorkspaceCommand) => void;
}): void {
  const runRef = useRef(onRun);
  runRef.current = onRun;
  // The panel on screen needs no "Show" row. A chord's own ⌘K row already runs a
  // command bound to it here, such as opening the default shell.
  const offered = commands.filter(
    (command) => !(command.kind === "show" && command.active) && command.shortcut === undefined,
  );
  // Rebuilt only when what the rows say changes, not on every render of the panel.
  const signature = JSON.stringify(
    offered.map((command) => [command.id, command.label, command.availability, command.shortcut]),
  );
  const offeredRef = useRef(offered);
  offeredRef.current = offered;
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      offeredRef.current.map((command) => ({
        // The session is part of the id, so a row built for another workspace never runs.
        id: `workspace:${conversationId}:${command.id}`,
        label: command.label,
        icon: workspaceCommandIcon(command),
        keywords: [...command.keywords],
        availability: command.availability,
        // A new panel or shell takes focus once the palette has closed.
        movesFocus: true,
        run: () => {
          const current = offeredRef.current.find((candidate) => candidate.id === command.id);
          if (current) runRef.current(current);
        },
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the signature covers `offered`.
    [conversationId, signature],
  );
  usePaletteCommands(`workspace:${conversationId}`, "Workspace", paletteCommands);
}
