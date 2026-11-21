// Shared workspace command labels, availability and ordering, for menus and the palette.

import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type { ShortcutId } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import type { DefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";
import { AVAILABLE, type CommandAvailability, unavailableBecause } from "@/store/paletteCommands";

import {
  backTarget,
  forwardTarget,
  type WorkspaceHistoryState,
} from "../workspaceTabs/workspaceHistory";
import type { Visit } from "../workspaceTabs/workspacePlace";

/** Whether a new shell can start now, from the session's liveness. */
export type ShellConnectState = "ready" | "wakeable" | "offline";

/**
 * Online sessions are ready. Asleep or starting ones wake as the shell starts.
 * An offline host can't be reconnected from here, so it can't open one.
 */
export function shellConnectState(liveness: SessionLiveness | undefined): ShellConnectState {
  switch (liveness?.kind) {
    case "host_offline":
    case "local_stranded":
      return "offline";
    case "runner_asleep":
    case "host_asleep":
    case "starting":
      return "wakeable";
    // Online, unknown, or not yet polled: never block on an unresolved poll.
    default:
      return "ready";
  }
}

export interface ShellFacts {
  /** The terminals the agent declares, in declaration order. */
  declared: readonly string[];
  /** The one a plain "new shell" opens: the remembered pick, else the first declared. */
  defaultShell: string;
  connect: ShellConnectState;
  /** A shell this workspace asked for is still starting. */
  launching: boolean;
  /** The viewer can edit the session, which opening a shell needs. */
  canEdit: boolean;
}

/** A panel already open in the workspace, in strip order. */
export interface OpenWorkspacePanel {
  key: string;
  label: string;
  active: boolean;
  /** It's closing, so it can't be shown. */
  closing: boolean;
  /** It has a close button the viewer may use. */
  closable: boolean;
  /** The name its close button uses, when it differs from `label` (file tabs). */
  closeName?: string;
}

/** What the workspace's actions need to know about it. */
export interface WorkspaceCommandFacts {
  /** Panels that open as tabs, with their labels; unavailable ones say why. */
  openablePanels: readonly {
    tab: DefaultWorkspaceTab;
    label: string;
    availability?: CommandAvailability;
  }[];
  browser: boolean;
  sideChat: boolean;
  /** `null` when the agent declares no terminals. */
  shells: ShellFacts | null;
  openPanels: readonly OpenWorkspacePanel[];
  /** Which of the new-shell and new-browser chords act on this workspace. */
  boundChords: { shell: boolean; browser: boolean };
  /** Whether the workspace fills the window; `null` when it can't go full screen. */
  fullScreen: boolean | null;
}

export type WorkspaceCommand =
  | (WorkspaceCommandBase & { kind: "panel"; tab: DefaultWorkspaceTab })
  | (WorkspaceCommandBase & { kind: "browser" | "side-chat" })
  | (WorkspaceCommandBase & {
      kind: "shell";
      shell: string;
      isDefault: boolean;
      /** Starting on a session that's waking up, which can take a while. */
      reconnecting: boolean;
      /** Its row in a list of every shell, such as "Choose shell": "zsh (default)". */
      choiceLabel: string;
    })
  | (WorkspaceCommandBase & { kind: "show"; panelKey: string; active: boolean })
  | (WorkspaceCommandBase & { kind: "close"; panelKey: string })
  | (WorkspaceCommandBase & { kind: "full-screen"; fullScreen: boolean })
  | (WorkspaceCommandBase & {
      kind: "history";
      direction: WorkspaceHistoryDirection;
      /** A caveat about where it lands, shown under its label. */
      note?: string;
    })
  | (WorkspaceCommandBase & { kind: "tab-action"; action: WorkspaceTabAction });

/** Actions on the tab design's list of tabs, rather than one tab's content. */
export type WorkspaceTabAction =
  | "new-tab"
  | "close-tab"
  | "reopen"
  | "close-others"
  | "move-left"
  | "move-right"
  | "toggle-pin"
  | "search-tabs";
export type WorkspaceTabActionCommand = Extract<WorkspaceCommand, { kind: "tab-action" }>;

export type WorkspaceShellCommand = Extract<WorkspaceCommand, { kind: "shell" }>;
export type WorkspaceShowCommand = Extract<WorkspaceCommand, { kind: "show" }>;
export type WorkspaceCloseCommand = Extract<WorkspaceCommand, { kind: "close" }>;
export type WorkspaceFullScreenCommand = Extract<WorkspaceCommand, { kind: "full-screen" }>;
export type WorkspaceHistoryCommand = Extract<WorkspaceCommand, { kind: "history" }>;

interface WorkspaceCommandBase {
  /** Stable for what it acts on, such as `shell:bash` or `show:terminal:…`. */
  id: string;
  /** The label in ⌘K, which names the action without its menu around it. */
  label: string;
  /** The label in its menu, under a heading such as "Open new". */
  menuLabel: string;
  keywords: readonly string[];
  shortcut?: ShortcutId;
  availability: CommandAvailability;
}

export const SHELL_OFFLINE_REASON = "This session is offline. Reconnect it to open a shell.";
export const SHELL_STARTING_REASON = "A shell is starting…";
export const SHELL_EDIT_ACCESS_REASON = "You need edit access to open a shell in this session.";
export const PANEL_CLOSING_REASON = "This panel is closing.";

/** A tab's close button and its ⌘K row share this label. */
export function closeTabLabel(label: string): string {
  return `Close ${label}`;
}

function shellAvailability(shells: ShellFacts): CommandAvailability {
  if (!shells.canEdit) return unavailableBecause(SHELL_EDIT_ACCESS_REASON);
  if (shells.connect === "offline") return unavailableBecause(SHELL_OFFLINE_REASON);
  if (shells.launching) return unavailableBecause(SHELL_STARTING_REASON);
  return AVAILABLE;
}

/** The default shell first, then the others in declaration order. */
export function shellCommands(shells: ShellFacts, chordsBound: boolean): WorkspaceShellCommand[] {
  const availability = shellAvailability(shells);
  const others = shells.declared.filter((name) => name !== shells.defaultShell);
  // A wakeable session reconnects as the shell starts, which can take a while.
  const reconnecting = shells.launching && shells.connect === "wakeable";
  return [
    {
      kind: "shell",
      id: "shell",
      shell: shells.defaultShell,
      isDefault: true,
      reconnecting,
      choiceLabel: `${shells.defaultShell} (default)`,
      label: `Open a new shell (${shells.defaultShell})`,
      menuLabel: reconnecting ? "Reconnecting…" : `Shell (${shells.defaultShell})`,
      keywords: ["workspace", "terminal", "shell", shells.defaultShell],
      shortcut: chordsBound ? "terminal.new" : undefined,
      availability,
    },
    ...others.map((name): WorkspaceShellCommand => ({
      kind: "shell",
      id: `shell:${name}`,
      shell: name,
      isDefault: false,
      reconnecting: false,
      choiceLabel: name,
      label: `Open a new ${name} shell`,
      menuLabel: name,
      keywords: ["workspace", "terminal", "shell", "choose shell", name],
      availability,
    })),
  ];
}

/** Everything the "+" menu opens, in its order: panels, browser, side chat, shells. */
export function workspaceOpenCommands(facts: WorkspaceCommandFacts): WorkspaceCommand[] {
  return [
    ...facts.openablePanels.map(({ tab, label, availability }): WorkspaceCommand => ({
      kind: "panel",
      id: `panel:${tab}`,
      tab,
      label: `Open the ${label} panel`,
      menuLabel: label,
      keywords: ["workspace", "panel", "tab", label],
      availability: availability ?? AVAILABLE,
    })),
    ...(facts.browser
      ? [
          {
            kind: "browser",
            id: "browser",
            label: "Open a new browser tab",
            menuLabel: "Browser",
            keywords: ["workspace", "browser", "web", "preview"],
            shortcut: facts.boundChords.browser ? "browser.new" : undefined,
            availability: AVAILABLE,
          } satisfies WorkspaceCommand,
        ]
      : []),
    ...(facts.sideChat
      ? [
          {
            kind: "side-chat",
            id: "side-chat",
            label: "Open a new side chat",
            menuLabel: "Side chat",
            keywords: ["workspace", "fork", "chat", "side"],
            availability: AVAILABLE,
          } satisfies WorkspaceCommand,
        ]
      : []),
    ...(facts.shells ? shellCommands(facts.shells, facts.boundChords.shell) : []),
  ];
}

/** One "Show …" per open panel; the picker lists them all, and ⌘K hides the one on screen. */
export function workspaceShowCommands(facts: WorkspaceCommandFacts): WorkspaceShowCommand[] {
  return facts.openPanels.map((panel) => ({
    kind: "show",
    id: `show:${panel.key}`,
    panelKey: panel.key,
    active: panel.active,
    label: `Show ${panel.label}`,
    menuLabel: panel.label,
    keywords: ["workspace", "panel", "tab", "switch", "select panel"],
    availability: panel.closing ? unavailableBecause(PANEL_CLOSING_REASON) : AVAILABLE,
  }));
}

/** One "Close …" per open tab that has a close button; ⌘K lists every one. */
export function workspaceCloseCommands(facts: WorkspaceCommandFacts): WorkspaceCloseCommand[] {
  return facts.openPanels
    .filter((panel) => panel.closable)
    .map((panel) => ({
      kind: "close",
      id: `close:${panel.key}`,
      panelKey: panel.key,
      label: closeTabLabel(panel.closeName ?? panel.label),
      menuLabel: closeTabLabel(panel.closeName ?? panel.label),
      keywords: ["workspace", "close", "tab", "panel", panel.label],
      availability: panel.closing ? unavailableBecause(PANEL_CLOSING_REASON) : AVAILABLE,
    }));
}

/** The full-screen toggle, in the label for what it does now; none where it can't toggle. */
export function workspaceFullScreenCommand(
  facts: Pick<WorkspaceCommandFacts, "fullScreen">,
): WorkspaceFullScreenCommand | null {
  if (facts.fullScreen === null) return null;
  const fullScreen = facts.fullScreen;
  return {
    kind: "full-screen",
    id: "full-screen",
    fullScreen,
    label: fullScreen ? "Exit workspace full screen" : "Make the workspace full screen",
    menuLabel: fullScreen ? "Exit full screen" : "Full screen",
    keywords: ["workspace", "full screen", "maximize", "expand", "restore", "panel"],
    availability: AVAILABLE,
  };
}

export type WorkspaceHistoryDirection = "back" | "forward";

/** Where Back or Forward would land, for its command and chevron. */
export interface WorkspaceHistoryStep {
  /** The title of the place it lands on; null when there is nowhere to go. */
  target: string | null;
  /** The place is a browser page still loading on a shell that can't step through pages exactly. */
  waitingForPage: boolean;
  /** The place is a browser page this shell reloads by URL rather than stepping to its entry. */
  inexactPage?: boolean;
}

export type WorkspaceHistoryFacts = Record<WorkspaceHistoryDirection, WorkspaceHistoryStep>;

export const HISTORY_WAITING_REASON = "Waiting for the page to load";
export const NOTHING_BACK_REASON = "There's nothing to go back to in the workspace.";
export const NOTHING_FORWARD_REASON = "There's nothing to go forward to in the workspace.";
export const INEXACT_PAGE_NOTE =
  "This version of the app can't step through pages exactly. Update it for exact page history.";
const INEXACT_PAGE_LIMITS = [
  INEXACT_PAGE_NOTE,
  "A click on the page while Back or Forward is still loading counts as where that step landed.",
  "If a page fails to load, the tab keeps its address and shows whatever the page shows.",
].join("\n");

/** A browser tab's info: its page, then, on an app without exact page history, that history's limits. */
export function browserTabInfo(pageInfo: string, exactPageHistory: boolean): string {
  return exactPageHistory ? pageInfo : `${pageInfo}\n\n${INEXACT_PAGE_LIMITS}`;
}

/**
 * Where Back and Forward would land in `history`. A visit `isBlocked` reports
 * (a page replaying on an older shell) disables the step rather than skipping it.
 */
export function workspaceHistoryFacts(
  history: WorkspaceHistoryState,
  describe: (visit: Visit) => string,
  isBlocked: (visit: Visit) => boolean = () => false,
  isInexactPage: (visit: Visit) => boolean = () => false,
): WorkspaceHistoryFacts {
  const step = (target: Visit | null): WorkspaceHistoryStep => ({
    target: target && !isBlocked(target) ? describe(target) : null,
    waitingForPage: target !== null && isBlocked(target),
    inexactPage: target !== null && isInexactPage(target),
  });
  return { back: step(backTarget(history)), forward: step(forwardTarget(history)) };
}

/** Go back and Go forward, labelled with where each lands; both stay listed when unavailable. */
export function workspaceHistoryCommands(facts: WorkspaceHistoryFacts): WorkspaceHistoryCommand[] {
  return (["back", "forward"] as const).map((direction) => {
    const { target, waitingForPage, inexactPage } = facts[direction];
    const back = direction === "back";
    const verb = back ? "Back" : "Forward";
    const unavailable = waitingForPage
      ? HISTORY_WAITING_REASON
      : back
        ? NOTHING_BACK_REASON
        : NOTHING_FORWARD_REASON;
    return {
      kind: "history",
      id: `history:${direction}`,
      direction,
      label: back ? "Go back in the workspace" : "Go forward in the workspace",
      menuLabel: target === null ? verb : `${verb} to ${target}`,
      keywords: [
        "workspace",
        "history",
        "navigate",
        "tab",
        back ? "back previous" : "forward next",
      ],
      shortcut: back ? "workspace.back" : "workspace.forward",
      availability: target === null ? unavailableBecause(unavailable) : AVAILABLE,
      ...(inexactPage ? { note: INEXACT_PAGE_NOTE } : {}),
    };
  });
}

/** What the tab design's list actions need to know. */
export interface WorkspaceTabListFacts {
  /** A closed tab can be reopened. */
  canReopen: boolean;
  /** The selected tab's position, and how many tabs there are. */
  selectedIndex: number;
  tabCount: number;
  /** The sidebar is on screen; New tab and Reopen show it, the others wait for it. */
  sidebarOpen?: boolean;
  /** Why the selected tab can't close, such as a shell without edit access; absent when it can. */
  selectedCloseRefusal?: string;
  /** The strip holds only the selected empty New tab: nothing in the sidebar is left to close. */
  selectedIsOnlyEmptyNewTab?: boolean;
  /** The selected tab's title, so Close names what it closes. */
  selectedLabel?: string;
  /** How many tabs are pinned, at the list's start; moves stop at their edge. */
  pinnedCount?: number;
  /** The selected tab is pinned, so the pin action unpins it. */
  selectedPinned?: boolean;
  /** Why the selected tab can't be pinned, such as a split; absent when it can. */
  selectedPinRefusal?: string;
}

export const SIDEBAR_CLOSED_REASON = "The workspace sidebar is closed.";

export const NO_CLOSED_TAB_REASON = "There's no closed workspace tab to reopen.";
export const NO_OTHER_TABS_REASON = "No other workspace tabs are open.";
export const FIRST_TAB_REASON = "This is the first workspace tab.";
export const LAST_TAB_REASON = "This is the last workspace tab.";
export const NO_TAB_REASON = "No workspace tab is selected.";
export const TAB_NOT_CLOSABLE_REASON = "You need edit access to close this shell.";
export const NO_TABS_REASON = "No workspace tabs are open.";
export const SEARCH_TABS_LABEL = "Search tabs…";
export const PINNED_EDGE_REASON = "Pinned tabs stay before the other tabs.";

/**
 * New tab, reopen, close others, move left or right, and search the open tabs, for "+"'s menu,
 * the tab menu, the strip's tab list and ⌘K.
 */
export function workspaceTabActionCommands(
  facts: WorkspaceTabListFacts,
): WorkspaceTabActionCommand[] {
  const command = (
    action: WorkspaceTabAction,
    label: string,
    menuLabel: string,
    keywords: readonly string[],
    availability: CommandAvailability,
    shortcut?: ShortcutId,
  ): WorkspaceTabActionCommand => ({
    kind: "tab-action",
    id: `tab-action:${action}`,
    action,
    label,
    menuLabel,
    keywords: ["workspace", "tab", ...keywords],
    availability,
    ...(shortcut ? { shortcut } : {}),
  });
  const selected = facts.selectedIndex >= 0;
  // Pinned tabs come first: each kind moves only among its own.
  const pinnedCount = facts.pinnedCount ?? 0;
  const firstIndex = facts.selectedPinned ? 0 : pinnedCount;
  const lastIndex = facts.selectedPinned ? pinnedCount - 1 : facts.tabCount - 1;
  const leftEdgeIsPins = !facts.selectedPinned && pinnedCount > 0;
  const rightEdgeIsPins = facts.selectedPinned === true && pinnedCount < facts.tabCount;
  const onScreen = (availability: CommandAvailability) =>
    facts.sidebarOpen === false ? unavailableBecause(SIDEBAR_CLOSED_REASON) : availability;
  return [
    command(
      "new-tab",
      "New workspace tab",
      "New tab",
      ["new", "open", "empty"],
      AVAILABLE,
      "workspace.newTab",
    ),
    command(
      "close-tab",
      "Close workspace tab",
      facts.selectedLabel ? closeTabLabel(facts.selectedLabel) : "Close tab",
      ["close", "current", ...(facts.selectedLabel ? [facts.selectedLabel] : [])],
      onScreen(
        !selected
          ? unavailableBecause(NO_TAB_REASON)
          : facts.selectedCloseRefusal
            ? unavailableBecause(facts.selectedCloseRefusal)
            : AVAILABLE,
      ),
      "workspace.closeTab",
    ),
    command(
      "reopen",
      "Reopen closed workspace tab",
      "Reopen closed tab",
      ["reopen", "restore", "undo close"],
      // Reopening shows the sidebar, as after its last tab closed.
      facts.canReopen ? AVAILABLE : unavailableBecause(NO_CLOSED_TAB_REASON),
      "workspace.reopenTab",
    ),
    command(
      "close-others",
      "Close other workspace tabs",
      "Close other tabs",
      ["close", "others"],
      onScreen(
        selected && facts.tabCount > 1 ? AVAILABLE : unavailableBecause(NO_OTHER_TABS_REASON),
      ),
    ),
    command(
      "move-left",
      "Move workspace tab left",
      "Move left",
      ["move", "reorder", "left"],
      onScreen(
        selected && facts.selectedIndex > firstIndex
          ? AVAILABLE
          : unavailableBecause(leftEdgeIsPins && selected ? PINNED_EDGE_REASON : FIRST_TAB_REASON),
      ),
    ),
    command(
      "move-right",
      "Move workspace tab right",
      "Move right",
      ["move", "reorder", "right"],
      onScreen(
        selected && facts.selectedIndex < lastIndex
          ? AVAILABLE
          : unavailableBecause(rightEdgeIsPins && selected ? PINNED_EDGE_REASON : LAST_TAB_REASON),
      ),
    ),
    command(
      "toggle-pin",
      facts.selectedPinned ? "Unpin workspace tab" : "Pin workspace tab",
      facts.selectedPinned ? "Unpin tab" : "Pin tab",
      ["pin", "unpin", "keep", "icon"],
      onScreen(
        !selected
          ? unavailableBecause(NO_TAB_REASON)
          : facts.selectedPinRefusal
            ? unavailableBecause(facts.selectedPinRefusal)
            : AVAILABLE,
      ),
    ),
    // It opens from the strip's tab list, so it waits for the sidebar like the strip's actions.
    command(
      "search-tabs",
      SEARCH_TABS_LABEL,
      "All tabs",
      ["search", "find", "switch", "go to", "list", "all tabs", "open tabs"],
      onScreen(facts.tabCount > 0 ? AVAILABLE : unavailableBecause(NO_TABS_REASON)),
    ),
  ];
}

/**
 * The command for `action` among `commands`, for a menu row or button that shows it. Every list
 * action is defined; a host only leaves out New tab and Search tabs it has no place for.
 */
export function tabActionCommandFor(
  commands: readonly WorkspaceTabActionCommand[],
  action: WorkspaceTabAction,
): WorkspaceTabActionCommand {
  const command = commands.find((candidate) => candidate.action === action);
  if (!command) throw new Error(`No workspace tab command for "${action}"`);
  return command;
}
