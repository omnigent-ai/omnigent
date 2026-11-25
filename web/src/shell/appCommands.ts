// The app-wide commands the ⌘K palette lists in its Actions group, and how a
// typed query filters them.

import { useMemo } from "react";
import { toast } from "sonner";
import {
  CalendarClockIcon,
  InboxIcon,
  type LucideIcon,
  PanelLeftIcon,
  PanelRightIcon,
  SettingsIcon,
  SparklesIcon,
  SquarePenIcon,
} from "lucide-react";

import {
  newSessionShortcutKeys,
  rightSidebarToggleShortcutKeys,
  settingsShortcutKeys,
  sidebarToggleShortcutKeys,
} from "@/components/KeyboardShortcut";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useLocation, useNavigate } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { canBootstrapQuickPick, writeUiFeatureOverride } from "@/lib/uiFeatureOverrides";
import { useSettingsRoute } from "@/shell/settingsNav";
import { settingsSearchCommand } from "@/shell/settingsSearch/settingsSearchCommand";

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
  /** Only the old palette's filter honors this; QuickPick lists every command. */
  searchOnly?: boolean;
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
  const { inSettings } = useSettingsRoute();
  const { pathname, search } = useLocation();
  const mobile = useIsMobileViewport();
  const info = useServerInfo();
  const canEnableQuickPick = canBootstrapQuickPick(info);
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
      settingsSearchCommand({
        settingsLocation: inSettings ? { pathname, search } : null,
        mobile,
        navigate,
      }),
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
      ...(canEnableQuickPick
        ? [
            {
              id: "enable-quick-pick-preview",
              label: "Enable feature preview: new QuickPick command palette",
              icon: SparklesIcon,
              keywords: [
                "feature preview",
                "preview",
                "quickpick",
                "quick pick",
                "command palette",
                "new palette",
                "feature flag",
              ],
              searchOnly: true,
              run: () => {
                if (writeUiFeatureOverride("quick_pick_palette", true)) location.reload();
                else toast.error("Couldn't save the feature preview on this device.");
              },
            },
          ]
        : []),
    ],
    [
      navigate,
      pathname,
      search,
      inSettings,
      mobile,
      onToggleLeftSidebar,
      onToggleRightSidebar,
      rightSidebarShortcutEnabled,
      canEnableQuickPick,
    ],
  );
}

/** Match labels or keywords ignoring case; blank queries omit search-only commands. */
export function filterAppCommands<
  Command extends { label: string; keywords: readonly string[]; searchOnly?: boolean },
>(commands: readonly Command[], query: string): readonly Command[] {
  const q = query.trim().toLowerCase();
  if (q === "")
    return commands.some((command) => command.searchOnly)
      ? commands.filter((command) => !command.searchOnly)
      : commands;
  return commands.filter(
    (command) =>
      command.label.toLowerCase().includes(q) ||
      command.keywords.some((keyword) => keyword.toLowerCase().includes(q)),
  );
}
