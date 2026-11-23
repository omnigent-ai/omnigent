// The kinds of tab the all-tabs list groups: what each holds, in the order it lists them, and
// which a whole group closes at once. Shells close one at a time, each asking about its process.

import type { WorkspaceTabGroupCloseAction } from "../workspaceCommands/model";

import type { WorkspaceTab } from "./workspaceTab";

export type TabGroupId = "pull-requests" | "files" | "shells" | "pages" | "other";

export interface TabGroup {
  id: TabGroupId;
  label: string;
  /** The command that closes the whole group, which its heading and ⌘K offer. */
  closeAll?: WorkspaceTabGroupCloseAction;
  /** The command that closes the group's tabs whose pull request has merged. */
  closeMerged?: WorkspaceTabGroupCloseAction;
}

export const TAB_GROUPS: readonly TabGroup[] = [
  {
    id: "pull-requests",
    label: "Pull requests",
    closeMerged: "close-merged-pull-requests",
    closeAll: "close-pull-request-tabs",
  },
  { id: "files", label: "Files", closeAll: "close-file-tabs" },
  { id: "shells", label: "Shells" },
  { id: "pages", label: "Pages", closeAll: "close-page-tabs" },
  // New tabs, agents, side chats and lists, which share nothing to close them by.
  { id: "other", label: "Other" },
];

export function tabGroup(id: TabGroupId): TabGroup {
  return TAB_GROUPS.find((group) => group.id === id)!;
}

/** The group closes that take a tab in `group`, given whether its pull request has merged. */
export function groupClosesOf(group: TabGroupId, merged: boolean): WorkspaceTabGroupCloseAction[] {
  const { closeAll, closeMerged } = tabGroup(group);
  return [...(closeMerged && merged ? [closeMerged] : []), ...(closeAll ? [closeAll] : [])];
}

/** The group `tab` is listed under. */
export function tabGroupOf(tab: WorkspaceTab): TabGroupId {
  if (tab.binding?.kind === "terminal") return "shells";
  switch (tab.place.kind) {
    case "pull":
      return "pull-requests";
    case "files":
    case "changes":
      return "files";
    case "terminal":
      return "shells";
    case "web":
      return "pages";
    default:
      return "other";
  }
}

/** The pull request a tab shows, itself or one of its files. */
export function tabPullRequestUrl(tab: WorkspaceTab): string | undefined {
  return tab.place.kind === "pull" ? tab.place.url : undefined;
}
