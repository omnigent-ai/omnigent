// The new-tab page's rows: the workspace's apps, in a fixed order, built from the
// same commands as the "+" menu and ⌘K so labels, availability and shortcuts
// can't drift. Picking an app either shows a place in the tab (Files, a blank
// browser page, a side-chat draft), runs a command (starting a shell), or opens
// the workspace text search. Other shells get their own rows; past a few,
// "Choose shell…" lists the rest.

import { isCommandListed, type CommandAvailability } from "@/store/paletteCommands";
import type { ShortcutId } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { WORKSPACE_TEXT_SEARCH_COPY } from "@/lib/workspaceTextSearchCopy";
import type { WorkspaceCommand, WorkspaceShellCommand } from "@/shell/workspaceCommands/model";
import type { DefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";

import type { WorkspacePlace } from "./workspacePlace";

export type NewTabAppId =
  | "terminal"
  | "choose-shell"
  | "files"
  | "changes"
  | "pulls"
  | "agents"
  | "tasks"
  | "side-chat"
  | "browser"
  | "search";

/** What picking a row does. */
export type NewTabAction =
  | { kind: "place"; place: WorkspacePlace }
  | { kind: "command"; command: WorkspaceCommand }
  /** Opens the workspace text search, as ⌘K and its shortcut do. */
  | { kind: "text-search" }
  /** Opens the list of every declared shell. */
  | { kind: "choose-shell" };

export interface NewTabEntry {
  id: NewTabAppId | `shell:${string}`;
  label: string;
  description?: string;
  keywords: readonly string[];
  shortcut?: ShortcutId;
  /** Set when the app can't open now; the row stays listed and says why. */
  disabledReason?: string;
  action: NewTabAction;
}

export interface NewTabFacts {
  /** The workspace's open commands (`workspaceOpenCommands`), as the "+" menu uses. */
  commands: readonly WorkspaceCommand[];
  /** Whether the workspace text search can open on this session; it explains there when it can't run. */
  textSearch: boolean;
}

const PANEL_PLACES: Record<DefaultWorkspaceTab, { id: NewTabAppId; place: WorkspacePlace }> = {
  files: { id: "files", place: { kind: "files" } },
  changes: { id: "changes", place: { kind: "changes" } },
  github: { id: "pulls", place: { kind: "pulls" } },
  subagents: { id: "agents", place: { kind: "agents" } },
  tasks: { id: "tasks", place: { kind: "tasks" } },
};

/** The order apps are listed in, whatever order the commands arrive in; other shells follow Terminal. */
const ORDER: readonly NewTabAppId[] = [
  "terminal",
  "side-chat",
  "changes",
  "files",
  "choose-shell",
  "pulls",
  "agents",
  "tasks",
  "browser",
  "search",
];

/** Other shells listed as their own rows; more than this and the rest wait behind "Choose shell…". */
export const SHELL_ROW_LIMIT = 3;

function orderOf(entry: NewTabEntry): number {
  // A shell row sits just before "Choose shell…", in the order the shells were declared.
  return ORDER.indexOf(entry.id.startsWith("shell:") ? "choose-shell" : (entry.id as NewTabAppId));
}

function reasonOf(availability: CommandAvailability): string | undefined {
  return availability.available ? undefined : availability.reason;
}

/** Shell commands this workspace lists; hidden ones (no reason to show) are left out, as in ⌘K. */
function shellCommandsOf(commands: readonly WorkspaceCommand[]): WorkspaceShellCommand[] {
  return commands.filter(
    (command): command is WorkspaceShellCommand =>
      command.kind === "shell" && isCommandListed(command.availability),
  );
}

/**
 * The root page's rows. Shells appear only when the agent declares some, as in the
 * "+" menu; a hidden command (unavailable with no reason) isn't listed at all.
 */
export function newTabEntries({ commands, textSearch }: NewTabFacts): NewTabEntry[] {
  const entries: NewTabEntry[] = [];
  const shells = shellCommandsOf(commands);
  const defaultShell = shells.find((command) => command.isDefault);
  if (defaultShell) {
    entries.push({
      id: "terminal",
      label: defaultShell.reconnecting ? "Terminal (reconnecting…)" : "Terminal",
      description: defaultShell.shell,
      keywords: defaultShell.keywords,
      // The new-shell chord, as the "+" menu and ⌘K show it.
      shortcut: defaultShell.shortcut,
      disabledReason: reasonOf(defaultShell.availability),
      action: { kind: "command", command: defaultShell },
    });
    const otherShells = shells.filter((command) => command !== defaultShell);
    for (const command of otherShells.slice(0, SHELL_ROW_LIMIT)) {
      // On the app list a shell's row says what it opens; the chooser's heading already does.
      entries.push({ ...shellEntry(command), description: "Terminal" });
    }
    if (otherShells.length > SHELL_ROW_LIMIT) {
      entries.push({
        id: "choose-shell",
        label: "Choose shell…",
        description: `${shells.length} shells`,
        keywords: ["terminal", "shell", "choose shell", ...shells.map((command) => command.shell)],
        disabledReason: reasonOf(defaultShell.availability),
        action: { kind: "choose-shell" },
      });
    }
  }
  for (const command of commands) {
    if (!isCommandListed(command.availability)) continue;
    switch (command.kind) {
      case "panel": {
        const { id, place } = PANEL_PLACES[command.tab];
        entries.push({
          id,
          label: command.menuLabel,
          keywords: command.keywords,
          disabledReason: reasonOf(command.availability),
          action: { kind: "place", place },
        });
        break;
      }
      case "browser":
        entries.push({
          id: "browser",
          label: "Browser",
          keywords: command.keywords,
          shortcut: command.shortcut,
          disabledReason: reasonOf(command.availability),
          action: { kind: "place", place: { kind: "web" } },
        });
        break;
      case "side-chat":
        entries.push({
          id: "side-chat",
          label: "Side chat",
          keywords: command.keywords,
          disabledReason: reasonOf(command.availability),
          action: { kind: "place", place: { kind: "chat" } },
        });
        break;
      default:
        break;
    }
  }
  if (textSearch) {
    entries.push({
      id: "search",
      label: WORKSPACE_TEXT_SEARCH_COPY.app,
      keywords: ["search", "find", "grep", "content", "text", "find in files"],
      shortcut: "palette.text",
      action: { kind: "text-search" },
    });
  }
  // A stable sort keeps the shells in their declared order.
  return entries.sort((left, right) => orderOf(left) - orderOf(right));
}

/** A shell's own row, named as its "+" menu item is. */
function shellEntry(command: WorkspaceShellCommand): NewTabEntry {
  return {
    id: `shell:${command.shell}`,
    label: command.choiceLabel,
    keywords: command.keywords,
    shortcut: command.shortcut,
    disabledReason: reasonOf(command.availability),
    action: { kind: "command", command },
  };
}

/** The "Choose shell…" page: every declared shell, the default first. */
export function shellChoiceEntries(commands: readonly WorkspaceCommand[]): NewTabEntry[] {
  return shellCommandsOf(commands).map(shellEntry);
}
