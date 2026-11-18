// The app-wide commands the ⌘K palette lists in its Actions group, and how a
// typed query filters them.

import { useMemo } from "react";
import {
  CalendarClockIcon,
  InboxIcon,
  type LucideIcon,
  PanelLeftIcon,
  PanelRightIcon,
  SettingsIcon,
  SquarePenIcon,
} from "lucide-react";

import {
  newSessionShortcutKeys,
  rightSidebarToggleShortcutKeys,
  settingsShortcutKeys,
  sidebarToggleShortcutKeys,
} from "@/components/KeyboardShortcut";
import { useNavigate } from "@/lib/routing";

export interface ActionCommand {
  id: string;
  label: string;
  /** Mirrors the icon on the equivalent button elsewhere in the UI. */
  icon: LucideIcon;
  /** Extra terms the client-side filter matches against (beyond the label). */
  keywords: string[];
  run: () => void;
  /** Chord shown beside the row when the same action has a shortcut. */
  shortcut?: readonly string[];
}

export interface AppCommandOptions {
  /** Flip the left (Conversations) sidebar, owned by AppShell. */
  onToggleLeftSidebar: () => void;
  /** Flip the right (Workspace) sidebar, owned by AppShell. */
  onToggleRightSidebar: () => void;
  /** False on grid routes, where ⌘⌥] belongs to the grid's own workspace. */
  rightSidebarShortcutEnabled: boolean;
}

/** Commands available everywhere, in the order the palette lists them. */
export function useAppCommands({
  onToggleLeftSidebar,
  onToggleRightSidebar,
  rightSidebarShortcutEnabled,
}: AppCommandOptions): ActionCommand[] {
  const navigate = useNavigate();
  return useMemo<ActionCommand[]>(
    () => [
      {
        id: "new-chat",
        label: "New chat",
        icon: SquarePenIcon,
        keywords: ["compose", "start", "new session"],
        run: () => navigate("/"),
        shortcut: newSessionShortcutKeys(),
      },
      {
        id: "go-inbox",
        label: "Go to Inbox",
        icon: InboxIcon,
        keywords: ["notifications", "comments", "needs response"],
        run: () => navigate("/inbox"),
      },
      {
        id: "go-tasks",
        label: "Go to Automations",
        icon: CalendarClockIcon,
        keywords: ["scheduled", "recurring", "cron", "automation", "schedule"],
        run: () => navigate("/tasks"),
      },
      {
        id: "go-settings",
        label: "Go to Settings",
        icon: SettingsIcon,
        keywords: ["preferences", "configuration", "account"],
        run: () => navigate("/settings"),
        shortcut: settingsShortcutKeys(),
      },
      {
        id: "toggle-left-sidebar",
        label: "Toggle conversations sidebar",
        icon: PanelLeftIcon,
        keywords: ["panel", "left", "sessions list"],
        run: onToggleLeftSidebar,
        shortcut: sidebarToggleShortcutKeys(),
      },
      {
        id: "toggle-right-sidebar",
        label: "Toggle workspace sidebar",
        icon: PanelRightIcon,
        keywords: ["panel", "right", "files", "terminal"],
        run: onToggleRightSidebar,
        shortcut: rightSidebarShortcutEnabled ? rightSidebarToggleShortcutKeys() : undefined,
      },
    ],
    [navigate, onToggleLeftSidebar, onToggleRightSidebar, rightSidebarShortcutEnabled],
  );
}

/** The commands whose label or a keyword contains `query`, ignoring case; all of them for a blank query. */
export function filterAppCommands(
  commands: readonly ActionCommand[],
  query: string,
): readonly ActionCommand[] {
  const q = query.trim().toLowerCase();
  if (q === "") return commands;
  return commands.filter(
    (command) =>
      command.label.toLowerCase().includes(q) ||
      command.keywords.some((keyword) => keyword.toLowerCase().includes(q)),
  );
}
