// A workspace tab: one place, an optional live resource it is bound to for its
// whole life, and who opened it. Pure helpers decide which places a tab may show.

import type { Opener, TabBinding as PlanBinding } from "./planOpen";
import type { BrowserEntry, TabId, WorkspacePlace } from "./workspacePlace";

/**
 * The live resource a bound tab shows; it never changes for the tab's life. A side
 * chat's `draftKey` names its composer until the first send creates `id`.
 */
export type TabBinding =
  Exclude<PlanBinding, { kind: "chat" }> | { kind: "chat"; id?: string; draftKey: string };

export type TabOpener = Opener;

/** An agent's open that waits for the user to show it. */
export interface PendingPlace {
  place: WorkspacePlace;
}

export interface WorkspaceTab {
  id: TabId;
  place: WorkspacePlace;
  binding?: TabBinding;
  /** The one Files tab, which navigates in place. */
  role?: "files";
  openedBy: TabOpener;
  attention?: PendingPlace;
  /** Something changed in the tab while it wasn't selected; the strip shows a dot. */
  unseen?: true;
  /** A browser tab's place in its page's own history, when the shell reports it. */
  entry?: BrowserEntry;
  /** Kept at the strip's start as an icon, and spared by Close other tabs. */
  pinned?: true;
}

/** A tab saved by a newer version of the app, kept as-is so it isn't lost. */
export interface UnknownWorkspaceTab {
  id: TabId;
  unknown: Record<string, unknown>;
}

export type WorkspaceTabRecord = WorkspaceTab | UnknownWorkspaceTab;

export function isUnknownTab(tab: WorkspaceTabRecord): tab is UnknownWorkspaceTab {
  return "unknown" in tab;
}

/** A closed tab that "Reopen closed tab" can bring back at its old position. */
export interface ClosedTab {
  tab: WorkspaceTab;
  index: number;
}

/** Why a tab refuses a place, as the refusal says it. */
export function refusalForTab(tab: WorkspaceTab): string {
  switch (tab.binding?.kind) {
    case "terminal":
      return "That tab shows a shell.";
    case "browser-view":
      return "That tab shows a web page.";
    case "chat":
      return "That tab shows a side chat.";
    case "agent":
      return "That tab shows an agent.";
    default:
      return "That tab can't show a shell, chat, agent or web page.";
  }
}
