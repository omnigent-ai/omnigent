// What the workspace rail shows for a selection: an open shell or file in front,
// over the selected page. A pure function, so any sidebar that restores the rail's
// selection shows exactly what the rail would.

import type { RightRailTab } from "./railTabs";

/** The page beneath any open shell or file. */
export type WorkspaceScopeView =
  "sidechat" | "browser" | "github" | "subagents" | "empty" | "content-search" | "files";

export interface WorkspaceSelection {
  rightRailTab: RightRailTab | null;
  selectedFilePath: string | null;
  selectedTerminalKey: string | null;
  selectedBrowserId: string | null;
  selectedSideChatId: string | null;
}

export interface WorkspaceViewAvailability {
  /** Whether the selected shell's terminal is present. */
  terminalPresent: (key: string) => boolean;
  showBrowserTab: boolean;
  showGithubTab: boolean;
  showContentSearch: boolean;
  hasAgentTree: boolean;
  /** Phones show side chats in their own drawer, not the rail. */
  mobile: boolean;
}

export interface WorkspaceActiveView {
  /** The shell shown in front, when its terminal is present. */
  terminalKey: string | null;
  /** The file shown in front when no shell is. */
  filePath: string | null;
  scope: WorkspaceScopeView;
  /** The Files page lists only changes. */
  flat: boolean;
  sideChatSelected: boolean;
  browserSelected: boolean;
}

export function resolveWorkspaceActiveView(
  selection: WorkspaceSelection,
  availability: WorkspaceViewAvailability,
): WorkspaceActiveView {
  const { rightRailTab, selectedFilePath, selectedTerminalKey } = selection;
  const terminalKey =
    selectedTerminalKey !== null && availability.terminalPresent(selectedTerminalKey)
      ? selectedTerminalKey
      : null;
  // A selected shell counts even while its terminal is missing, so a page doesn't flash in.
  const nothingInFront = selectedFilePath === null && selectedTerminalKey === null;
  const sideChatSelected =
    rightRailTab === "sidechat" && nothingInFront && selection.selectedSideChatId !== null;
  const browserSelected =
    rightRailTab === "browser" && nothingInFront && selection.selectedBrowserId !== null;
  const scope: WorkspaceScopeView =
    sideChatSelected && !availability.mobile
      ? "sidechat"
      : browserSelected && availability.showBrowserTab
        ? "browser"
        : rightRailTab === "github" && availability.showGithubTab
          ? "github"
          : rightRailTab === "subagents" && availability.hasAgentTree
            ? "subagents"
            : rightRailTab === null
              ? "empty"
              : rightRailTab === "content-search" && availability.showContentSearch
                ? "content-search"
                : "files";
  return {
    terminalKey,
    filePath: terminalKey === null ? selectedFilePath : null,
    scope,
    flat: rightRailTab === "changes",
    sideChatSelected,
    browserSelected,
  };
}
