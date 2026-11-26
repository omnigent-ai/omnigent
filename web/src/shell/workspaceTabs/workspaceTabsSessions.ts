// The workspace sidebar's per-session state for this app run: each session's tab store,
// created from saved tabs on first use, the
// unsaved-changes guards its open editors register, and the file positions a
// pending open asked for. Every entry point opens through here, so the panel,
// chat links and ⌘K share one implementation.

import { randomUUID } from "@/lib/randomUUID";
import { recordRecentWorkspaceFile } from "@/lib/recentWorkspaceFiles";
import { toast } from "sonner";

import type { FilePosition } from "@/shell/FileViewerContext";
import { citedFilePosition } from "@/shell/filePositionState";
import { reviewMembership, settledShownReview } from "@/lib/reviewFileLinks";
import { browseLocationChanges } from "@/shell/filesPane/browseLocations";
import { sessionChoiceCount } from "@/lib/workingCopyComparisons";

import { type OpenOptions, samePlace } from "./planOpen";
import { pullRequestTabPlace } from "./pullRequestSubpage";
import type { LineRange, TabId, WorkspaceLink, WorkspacePlace } from "./workspacePlace";
import { isUnknownTab } from "./workspaceTab";
import {
  readWorkspaceTabsSession,
  saveWorkspaceTabsSession,
  type WorkspaceTabsSessionState,
} from "./workspaceTabsPersistence";
import type { PullRequestsTarget, WorkspaceFileOpenOptions } from "./workspacePlaceNavigation";
import {
  createWorkspaceTabsStore,
  type GuardedResult,
  isOnlyEmptyNewTab,
  type LeaveReason,
  type OpenResult,
  type WorkspaceTabsStore,
} from "./workspaceTabsStore";

export const SAVE_FAILED_MESSAGE =
  "Couldn't save your workspace tabs in this browser or app. They'll reset when you reload.";

/** An editor's question about its unsaved edits: "leave" discards them, "cancel" keeps editing. */
export type LeaveGuard = () => Promise<"leave" | "cancel">;

/**
 * A file open's cited position, by identity, for the tab that shows it. It lasts while the
 * tab shows the place it was cited for; after that the place's own lines apply.
 */
export interface FileNavigationRequest {
  position?: FilePosition;
}

export interface WorkspaceTabsSession {
  store: WorkspaceTabsStore;
  /** Register an open editor's guard while it has unsaved edits. */
  registerLeaveGuard: (tabId: TabId, guard: LeaveGuard) => () => void;
  /** Tabs with unsaved edits, which stay mounted while another tab is selected. */
  dirtyTabIds: () => ReadonlySet<TabId>;
  requestLeave: (tabIds: TabId[], reason: LeaveReason) => Promise<"leave" | "cancel">;
  fileRequest: (tabId: TabId) => FileNavigationRequest | undefined;
  setFileRequest: (tabId: TabId, request: FileNavigationRequest) => void;
  /** Told when a guard registers or goes, or a file request changes. */
  subscribe: (listener: () => void) => () => void;
  /** Moves on every change `subscribe` reports. */
  version: () => number;
  /** The new-tab page a shell launch came from, which the shell then fills. */
  setPendingShellTab: (tabId: TabId | null) => void;
  takePendingShellTab: () => TabId | null;
}

const sessions = new Map<string, WorkspaceTabsSession>();

/** A session the sidebar shows for the first time: no tabs, so it opens on a new tab. */
const EMPTY_SESSION: WorkspaceTabsSessionState = {
  tabs: [],
  selectedTabId: null,
  timeline: [],
  cursor: -1,
  closed: [],
};

function createSession(sessionId: string): WorkspaceTabsSession {
  const guards = new Map<TabId, LeaveGuard>();
  const listeners = new Set<() => void>();
  const fileRequests = new Map<TabId, { request: FileNavigationRequest; place: WorkspacePlace }>();
  let version = 0;
  let pendingShellTab: TabId | null = null;
  const notify = () => {
    version += 1;
    for (const listener of [...listeners]) listener();
  };
  const requestLeave = async (tabIds: TabId[]): Promise<"leave" | "cancel"> => {
    // One question per dirty editor, in strip order; the first "Keep editing" stops.
    for (const tabId of tabIds) {
      const guard = guards.get(tabId);
      // oxlint-disable-next-line no-await-in-loop -- one unsaved-changes dialog at a time
      if (guard && (await guard()) === "cancel") return "cancel";
    }
    return "leave";
  };

  const saved = readWorkspaceTabsSession(sessionId);
  const newTabId = () => randomUUID();
  const initial = saved.status === "saved" ? saved.state : EMPTY_SESSION;
  const save =
    saved.status === "read-only"
      ? null
      : (state: Parameters<typeof saveWorkspaceTabsSession>[1]) =>
          saveWorkspaceTabsSession(sessionId, state);
  const store = createWorkspaceTabsStore({
    scopeId: sessionId,
    initial,
    save,
    requestLeave,
    isTabDirty: (tabId) => guards.has(tabId),
    onSaveFailed: () => toast.error(SAVE_FAILED_MESSAGE),
    newTabId,
    choiceCount: () => sessionChoiceCount(sessionId),
    // Go to File lists first the files a tab opened, from the tree, Search or anywhere else.
    onFileOpened: (path) => recordRecentWorkspaceFile(sessionId, path),
    folderCount: () => browseLocationChanges(sessionId),
  });
  const currentPlace = (tabId: TabId) => {
    const tab = store.findTab(tabId);
    return tab && !isUnknownTab(tab) ? tab.place : undefined;
  };
  // A citation ends when its tab moves on, by a navigation, Back or Forward, or closing.
  store.subscribe(() => {
    for (const [tabId, { place }] of fileRequests) {
      const shown = currentPlace(tabId);
      if (!shown || !samePlace(shown, place)) fileRequests.delete(tabId);
    }
  });
  return {
    store,
    registerLeaveGuard(tabId, guard) {
      guards.set(tabId, guard);
      // Gaining unsaved edits changes what every pending guarded action must ask.
      store.noteDirtyChange(tabId);
      notify();
      return () => {
        if (guards.get(tabId) !== guard) return;
        guards.delete(tabId);
        store.noteDirtyChange(tabId);
        notify();
      };
    },
    dirtyTabIds: () => new Set(guards.keys()),
    requestLeave: (tabIds, reason) => store.askToLeave(tabIds, reason),
    fileRequest: (tabId) => fileRequests.get(tabId)?.request,
    setFileRequest(tabId, request) {
      const place = currentPlace(tabId);
      if (place) fileRequests.set(tabId, { request, place });
      else fileRequests.delete(tabId);
      notify();
    },
    version: () => version,
    setPendingShellTab(tabId) {
      pendingShellTab = tabId;
    },
    takePendingShellTab() {
      const tabId = pendingShellTab;
      pendingShellTab = null;
      return tabId;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** This session's tabs, created on first use from saved tabs. */
export function workspaceTabsSession(sessionId: string): WorkspaceTabsSession {
  let session = sessions.get(sessionId);
  if (!session) {
    session = createSession(sessionId);
    sessions.set(sessionId, session);
  }
  return session;
}

/** This session's tabs if the sidebar already used them this run; never creates them. */
export function existingWorkspaceTabsSession(sessionId: string): WorkspaceTabsSession | undefined {
  return sessions.get(sessionId);
}

/** Forget every session, so a test starts from storage again. */
// Each session's latest file link, so a later click wins over one still waiting.
const latestFileLinks = new Map<string, object>();

export function resetWorkspaceTabsSessionsForTests(): void {
  sessions.clear();
  latestFileLinks.clear();
}

/** The file position a place's cited lines stand for. */
function linePosition(line: LineRange): FilePosition {
  return {
    line: line.start,
    ...(line.end !== undefined ? { endLine: line.end } : {}),
    ...(line.column !== undefined ? { column: line.column } : {}),
  };
}

function lineRange(options?: WorkspaceFileOpenOptions): LineRange | undefined {
  const position = citedFilePosition(options);
  if (!position) return undefined;
  return {
    start: position.line,
    ...(position.endLine !== undefined ? { end: position.endLine } : {}),
    ...(position.column !== undefined ? { column: position.column } : {}),
  };
}

/**
 * Open a file: the tab already showing it, else a new file tab. The Files tab never turns
 * into the file here. A request for its diff opens the Changes review at the file instead.
 */
export async function openWorkspaceFile(
  sessionId: string,
  path: string,
  fileOptions: WorkspaceFileOpenOptions | undefined,
  opener: OpenOptions["opener"],
): Promise<OpenResult> {
  const session = workspaceTabsSession(sessionId);
  const { store } = session;
  const line = lineRange(fileOptions);
  const comment = fileOptions?.commentId;
  if (fileOptions?.view === "diff" && comment === undefined) {
    const result = await store.open(
      { place: { kind: "changes", path, ...(line ? { line } : {}) } },
      { opener },
    );
    if (result.ok)
      session.setFileRequest(result.tabId, { position: citedFilePosition(fileOptions) });
    return result;
  }
  const place: WorkspacePlace = {
    kind: "files",
    path,
    ...(line ? { line } : {}),
    ...(comment !== undefined ? { comment } : {}),
  };
  const showing = store
    .tabs()
    .find(
      (tab) =>
        !("unknown" in tab) &&
        tab.role !== "files" &&
        tab.place.kind === "files" &&
        tab.place.path === path,
    );
  // A selected Files tab with no file open shows the file rather than a new tab beside it.
  const selected = store.selectedTab();
  const selectedIsEmptyFiles =
    selected !== undefined &&
    !("unknown" in selected) &&
    selected.place.kind === "files" &&
    selected.place.path === undefined;
  const result = await store.open(
    { place },
    {
      opener,
      disposition:
        showing || opener === "agent" ? undefined : selectedIsEmptyFiles ? "current" : "new",
    },
  );
  if (result.ok) session.setFileRequest(result.tabId, { position: citedFilePosition(fileOptions) });
  return result;
}

/**
 * Open a place view's link. A file or review it lands on takes the link's cited lines as a
 * fresh request, or none, so an earlier request there never comes back.
 */
export async function openWorkspaceLink(
  sessionId: string,
  link: WorkspaceLink,
  options: OpenOptions,
): Promise<OpenResult> {
  const session = workspaceTabsSession(sessionId);
  const result = await session.store.open(link, options);
  const { place } = link;
  if (result.ok && (place.kind === "files" || place.kind === "changes")) {
    session.setFileRequest(result.tabId, place.line ? { position: linePosition(place.line) } : {});
  }
  return result;
}

/**
 * Open a file link, such as a chat citation: a Changes review on screen keeps a file it
 * shows and scrolls to the line, and any other file opens as `openWorkspaceFile` opens it.
 * While the review's comparison is still being worked out, the link waits for it.
 */
export async function openWorkspaceFileLink(
  sessionId: string,
  path: string,
  fileOptions: WorkspaceFileOpenOptions | undefined,
  opener: OpenOptions["opener"],
): Promise<OpenResult | GuardedResult> {
  const session = workspaceTabsSession(sessionId);
  const { store } = session;
  const reviewTab = () => {
    const selected = store.selectedTab();
    if (!selected || isUnknownTab(selected) || selected.place.kind !== "changes") return undefined;
    return { id: selected.id, place: selected.place };
  };
  const click = {};
  latestFileLinks.set(sessionId, click);
  // A comment's thread shows on the file itself, never in the review.
  if (!reviewTab() || fileOptions?.commentId !== undefined) {
    return openWorkspaceFile(sessionId, path, fileOptions, opener);
  }
  const planned = store.revision();
  const review = await settledShownReview(sessionId);
  // A later link, or the person moving on while the comparison settled, replaces this one.
  if (latestFileLinks.get(sessionId) !== click || store.revision() !== planned) {
    return { ok: false, stale: true };
  }
  const selected = reviewTab();
  if (!selected || !review || reviewMembership(review, path) !== "included") {
    return openWorkspaceFile(sessionId, path, fileOptions, opener);
  }
  const line = lineRange(fileOptions);
  const { range, comparison } = selected.place;
  const result = await store.navigate(selected.id, {
    kind: "changes",
    path,
    ...(line ? { line } : {}),
    ...(range ? { range } : {}),
    ...(comparison ? { comparison } : {}),
  });
  // A fresh request each time, so the same line cited again is brought back into view.
  if (result.ok) session.setFileRequest(selected.id, { position: citedFilePosition(fileOptions) });
  return result;
}

const PAGE_PLACES = {
  files: { kind: "files" },
  changes: { kind: "changes" },
  github: { kind: "pulls" },
  subagents: { kind: "agents" },
  tasks: { kind: "tasks" },
  "content-search": { kind: "search" },
} as const satisfies Record<string, WorkspacePlace>;

export type WorkspacePage = keyof typeof PAGE_PLACES;

export function isWorkspacePage(value: string | null | undefined): value is WorkspacePage {
  return value != null && value in PAGE_PLACES;
}

/** Show a built-in page (Files, Changes, Pull requests, Agents, Tasks or Search) in its tab. */
export function openWorkspacePage(
  sessionId: string,
  page: WorkspacePage,
  opener: OpenOptions["opener"] = "user",
): Promise<OpenResult> {
  return workspaceTabsSession(sessionId).store.open({ place: PAGE_PLACES[page] }, { opener });
}

/** A new-tab page at the end of the strip, as "+" opens one. */
export function openWorkspaceNewTab(sessionId: string): Promise<OpenResult> {
  return workspaceTabsSession(sessionId).store.open(
    { place: { kind: "new" } },
    { opener: "user", disposition: "new", position: "end" },
  );
}

/** The sidebar holds only an empty New tab, so showing it focuses that tab's search. */
export function showsOnlyEmptyNewTab(sessionId: string): boolean {
  return isOnlyEmptyNewTab(workspaceTabsSession(sessionId).store.tabs());
}

/** Show a shell in its tab; a launch from a new-tab page turns that tab into the shell. */
export function openWorkspaceTerminal(
  sessionId: string,
  key: string,
  opener: OpenOptions["opener"] = "user",
): Promise<OpenResult> {
  const session = workspaceTabsSession(sessionId);
  const newTabId = session.takePendingShellTab();
  const place: WorkspacePlace = { kind: "terminal", key };
  if (newTabId !== null) {
    const filled = session.store.fillNewTab(newTabId, place);
    if (filled !== null)
      return Promise.resolve({ ok: true, tabId: filled, created: true, selected: true });
  }
  return session.store.open({ place }, { opener });
}

/** Show a browser view in its tab, opening one for it when none is open. */
export function openWorkspaceBrowserView(
  sessionId: string,
  viewKey: string,
  { background }: { background: boolean },
): TabId | null {
  return workspaceTabsSession(sessionId).store.openBrowserView(viewKey, {
    select: !background,
    opener: background ? "agent" : "user",
  });
}

/**
 * Show all of a session's pull requests, or one of them, in its own tab: the tab
 * already showing it as it is (selected if it isn't), else a new one.
 */
export async function showWorkspacePullRequests(
  sessionId: string,
  target: PullRequestsTarget,
  { background = false }: { background?: boolean } = {},
): Promise<OpenResult> {
  const place: WorkspacePlace =
    target.kind === "overview" ? { kind: "pulls" } : pullRequestTabPlace(target.url, target.view);
  const { store } = workspaceTabsSession(sessionId);
  const result = await store.open(
    { place },
    { opener: "user", disposition: "focus", keepSelection: background },
  );
  // Its tab kept the sub-view it was left on; asked for its Overview, it steps back to it.
  if (target.kind === "pull" && target.overview && result.ok && !result.created) {
    const shown = store.findTab(result.tabId);
    if (shown?.place.kind === "pull" && shown.place.view !== "summary") {
      await store.navigate(result.tabId, place);
    }
  }
  return result;
}
