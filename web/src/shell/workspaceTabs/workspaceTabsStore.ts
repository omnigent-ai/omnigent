// One session's workspace tabs: their order, the selected tab, opening, navigating,
// closing and reopening, and saving. Every mutation bumps `revision`; an action that
// waits (the unsaved-changes guard, a shell's close dialog) re-checks it before it
// applies, so a newer action by the user always wins over an older one.

import { randomUUID } from "@/lib/randomUUID";
import { newPendingSideChatId } from "@/lib/sideChat";

import { isPinnedTab, withPinnedFirst } from "./pinnedTabs";
import { createShellWaits } from "./shellWaits";
import { type HistoryMoveOptions, WorkspaceHistory } from "./workspaceHistory";
import {
  bindingForPlace,
  canShow,
  type OpenOptions,
  type OpenPlan,
  type OpenState,
  type PlanTab,
  planOpen,
  type ResourceState,
  samePlace,
} from "./planOpen";
import {
  placeIdentity,
  type TabId,
  type Visit,
  type WorkspaceLink,
  type WorkspacePlace,
} from "./workspacePlace";
import {
  type ClosedTab,
  isUnknownTab,
  refusalForTab,
  type TabBinding,
  type TabOpener,
  type WorkspaceTab,
  type WorkspaceTabRecord,
} from "./workspaceTab";
import {
  MAX_CLOSED_TABS,
  MAX_WORKSPACE_TABS,
  type SaveSessionResult,
  type WorkspaceTabsSessionState,
} from "./workspaceTabsPersistence";

export type { OpenOptions, ResourceState };

export interface Refusal {
  ok: false;
  reason: string;
}
const STALE_RESULT = { ok: false, stale: true } as const;

export type GuardedResult =
  { ok: true } | Refusal | { ok: false; cancelled: true } | { ok: false; stale: true };
export type OpenResult =
  | { ok: true; tabId: TabId; created: boolean; selected: boolean }
  /** The link is for another session's sidebar; open it there. */
  | { ok: false; otherSession: Extract<OpenPlan, { action: "other-session" }> }
  | Exclude<GuardedResult, { ok: true }>;

export type LeaveReason = "navigate" | "close";
/** Asks about unsaved edits in `tabIds`; resolves "cancel" to keep them. */
export type RequestLeave = (tabIds: TabId[], reason: LeaveReason) => Promise<"leave" | "cancel">;

/** What the session's shell list says: not loaded (or failing), or the shells it has. */
export type TerminalInventory =
  { status: "unknown" } | { status: "loaded"; keys: readonly string[] };

export const MISSING_TAB_REASON = "No workspace tab has that ID.";
export const RECENT_GONE_REASON = "That place is no longer available.";

/** A place the new-tab page offers to go back to: a closed tab, or another open one. */
export interface RecentPlace {
  /** `placeIdentity` of the place; stable while the list changes. */
  identity: string;
  place: WorkspacePlace;
  /** The open tab showing it, when it's open. */
  openTabId?: TabId;
}

/** How many recent places the new-tab page lists. */
export const MAX_RECENT_PLACES = 5;
export const ALREADY_CLOSING_REASON = "Those shells are already closing.";
export const LAST_NEW_TAB_REASON = "This is the last tab. To hide the sidebar, use its toggle.";

/** Whether a batch close takes `tab`: pinned tabs and shells stay, as each closes on its own. */
export function isBatchClosable(tab: WorkspaceTabRecord): boolean {
  return !isPinnedTab(tab) && (isUnknownTab(tab) || tab.binding?.kind !== "terminal");
}

/**
 * The strip holds only an empty New tab, the sidebar's resting state: it can't close,
 * and a reopened or restored tab takes its place. A New tab on its shells page is not empty.
 */
export function isOnlyEmptyNewTab(tabs: readonly WorkspaceTabRecord[], tabId?: TabId): boolean {
  if (tabs.length !== 1) return false;
  const [only] = tabs;
  return (
    !isUnknownTab(only) &&
    only.place.kind === "new" &&
    only.place.app === undefined &&
    (tabId === undefined || only.id === tabId)
  );
}

/** The visit a tab shows now, with its page's history entry. */
export function visitOf(tab: WorkspaceTab): Visit {
  return tab.entry
    ? { tabId: tab.id, place: tab.place, entry: tab.entry }
    : { tabId: tab.id, place: tab.place };
}

/** A browser tab showing `visit`'s page and entry; no owner means no page to move. */
function showingVisit(tab: WorkspaceTab, visit: Visit): WorkspaceTab {
  const { entry: _entry, ...rest } = tab;
  return { ...rest, place: visit.place, ...(visit.entry ? { entry: visit.entry } : {}) };
}

/** `tab` showing `place`; a browser page that changes is a new page, so its old entry goes. */
function withPlace(tab: WorkspaceTab, place: WorkspacePlace): WorkspaceTab {
  if (tab.binding?.kind !== "browser-view" || samePlace(tab.place, place)) return { ...tab, place };
  return showingVisit(tab, { tabId: tab.id, place });
}

export interface WorkspaceTabsSnapshot {
  tabs: readonly WorkspaceTabRecord[];
  selectedTabId: TabId | null;
  revision: number;
  canReopen: boolean;
}

/**
 * What the sidebar shows with no tabs: a session's sidebar always keeps a New tab; a page's
 * sidebar that only opens links has none, and its host hides it.
 */
export type WorkspaceTabsResting = "new-tab" | "empty";

export interface WorkspaceTabsStoreOptions {
  /** Whose tabs these are: a session's ID, or a page's own key for a sidebar without one. */
  scopeId: string;
  /** Defaults to "new-tab". */
  resting?: WorkspaceTabsResting;
  initial: WorkspaceTabsSessionState;
  /** Save the state; null when this session must not be saved (a newer app owns storage). */
  save: ((state: WorkspaceTabsSessionState) => SaveSessionResult) | null;
  requestLeave: RequestLeave;
  /** Whether a tab holds unsaved edits right now. */
  isTabDirty?: (tabId: TabId) => boolean;
  /** Told once when saving fails, so the user learns their tabs won't survive a reload. */
  onSaveFailed?: () => void;
  /** Told on every revision change, so a wider action can tell it went stale. */
  onRevision?: () => void;
  history?: WorkspaceHistory;
  newTabId?: () => TabId;
  newViewKey?: () => string;
  newDraftKey?: () => string;
  /** How many comparisons the person has chosen for the session, stamped on each landing. */
  choiceCount?: () => number;
}

/** A line, column or comment move inside the same file, which history records in place. */
function isPositionOnlyMove(from: WorkspacePlace, to: WorkspacePlace): boolean {
  if (from.kind !== to.kind || (from.kind !== "files" && from.kind !== "changes")) return false;
  const strip = (place: WorkspacePlace) => {
    const {
      line: _line,
      comment: _comment,
      ...rest
    } = place as { line?: unknown; comment?: unknown };
    return rest;
  };
  return (
    (from as { path?: string }).path !== undefined &&
    samePlace(strip(from) as WorkspacePlace, strip(to) as WorkspacePlace)
  );
}

function withoutMarks(tab: WorkspaceTab): WorkspaceTab {
  const { attention: _attention, unseen: _unseen, ...rest } = tab;
  return rest;
}

function sameOrder(left: readonly WorkspaceTabRecord[], right: readonly WorkspaceTabRecord[]) {
  return left.length === right.length && left.every((tab, index) => tab === right[index]);
}

function withoutUnseen(tab: WorkspaceTab): WorkspaceTab {
  const { unseen: _unseen, ...rest } = tab;
  return rest;
}

export type WorkspaceTabsStore = ReturnType<typeof createWorkspaceTabsStore>;

/** A tab's landing on a visit; its view marks it `settled` once it showed what the visit asks. */
export interface TabLanding {
  settled: boolean;
  /** The session's choice count as it landed; a pick since is newer than the visit. */
  choices: number;
}

export function createWorkspaceTabsStore(options: WorkspaceTabsStoreOptions) {
  const {
    scopeId,
    resting = "new-tab",
    save,
    requestLeave,
    isTabDirty = () => false,
    onSaveFailed,
    onRevision,
    history = new WorkspaceHistory(),
    newTabId = () => randomUUID(),
    newViewKey = () => randomUUID(),
    newDraftKey = newPendingSideChatId,
    choiceCount = () => 0,
  } = options;

  let tabs: WorkspaceTabRecord[] = withPinnedFirst([...options.initial.tabs]);
  // Each tab's latest landing on a visit by a step, a link or an open, which its view settles once.
  const landings = new Map<TabId, TabLanding>();
  const land = (tabId: TabId) => landings.set(tabId, { settled: false, choices: choiceCount() });
  let selectedTabId: TabId | null = options.initial.selectedTabId;
  let closed: ClosedTab[] = [...options.initial.closed];
  let revision = 0;
  // Dirty-state changes bump the revision too. One made by answering the very
  // question being asked (discarding the edits) is excused, so it isn't "newer".
  let excusedChanges = 0;
  const asking = new Set<TabId>();
  /** The revision a pending action compares against. */
  const stamp = () => revision - excusedChanges;
  let saveFailureReported = false;
  let terminalInventory: TerminalInventory = { status: "unknown" };
  const shellWaits = createShellWaits();
  // Shells seen in a loaded list this run, or saved from an earlier one. Only those
  // can end; a shell that is still starting stays "Connecting…" until it appears.
  const seenTerminals = new Set<string>(
    options.initial.tabs.flatMap((tab) =>
      !isUnknownTab(tab) && tab.binding?.kind === "terminal" ? [tab.binding.key] : [],
    ),
  );
  const endedTerminals = new Set<string>();
  // Shells a group close is ending: the list catching up on them isn't a newer change.
  const closingTerminals = new Set<string>();
  // Side-chat and sub-agent sessions, as their last lookup said.
  const sessionStates = new Map<string, ResourceState>();
  // Most recently selected first, for "the most recently selected tab showing that file".
  let recentlySelected: TabId[] = selectedTabId === null ? [] : [selectedTabId];
  const listeners = new Set<() => void>();
  // Inside a transaction, commits wait for its end, so a step and its landing publish once.
  let transactionDepth = 0;
  let commitPending = false;

  history.restore({ visits: options.initial.timeline, cursor: options.initial.cursor }, (tabId) =>
    options.initial.tabs.some((tab) => tab.id === tabId),
  );
  // A session saved or migrated with no tabs shows a New tab, as closing the last one does.
  if (tabs.length === 0 && resting === "new-tab") addEmptyNewTab();

  const knownTabs = (): WorkspaceTab[] =>
    tabs.filter((tab): tab is WorkspaceTab => !isUnknownTab(tab));
  const findTab = (tabId: TabId): WorkspaceTab | undefined =>
    knownTabs().find((tab) => tab.id === tabId);
  const indexOf = (tabId: TabId) => tabs.findIndex((tab) => tab.id === tabId);

  const makeSnapshot = (): WorkspaceTabsSnapshot => ({
    tabs,
    selectedTabId,
    revision,
    canReopen: closed.some((entry) => entry.tab.binding?.kind !== "terminal"),
  });
  let snapshot = makeSnapshot();

  function serialize(): WorkspaceTabsSessionState {
    const { visits, cursor } = history.serialize();
    return { tabs, selectedTabId, timeline: visits, cursor, closed };
  }

  function commit() {
    if (transactionDepth > 0) {
      commitPending = true;
      return;
    }
    // A session's sidebar is never empty: whatever removed the last tab leaves a New tab.
    if (tabs.length === 0 && resting === "new-tab") addEmptyNewTab();
    // Whatever moved, opened or came back, pinned tabs stay first.
    tabs = withPinnedFirst(tabs);
    // No tab sits unselected.
    if (selectedTabId === null) selectFirstArrival();
    revision += 1;
    onRevision?.();
    snapshot = makeSnapshot();
    if (save) {
      const result = save(serialize());
      if (result === "failed" && !saveFailureReported) {
        saveFailureReported = true;
        onSaveFailed?.();
      }
    }
    for (const listener of [...listeners]) listener();
  }

  function addEmptyNewTab() {
    // Opened by the store, not the user: it shows without taking focus from where it is.
    const tab: WorkspaceTab = { id: newTabId(), place: { kind: "new" }, openedBy: "restore" };
    tabs = [tab];
    markSelected(tab.id);
    history.recordStep(visitOf(tab));
  }

  /**
   * Remove an only empty New tab, the empty sidebar, before another tab arrives in its place;
   * no history is left. With nothing selected, the commit shows the tab that arrived.
   */
  function takeEmptyNewTabsPlace() {
    if (!isOnlyEmptyNewTab(tabs)) return;
    const [placeholder] = tabs;
    tabs = [];
    recentlySelected = recentlySelected.filter((id) => id !== placeholder.id);
    history.removeTab(placeholder.id);
    if (selectedTabId === placeholder.id) selectedTabId = null;
  }

  function selectFirstArrival() {
    const first = knownTabs()[0];
    if (!first) return;
    markSelected(first.id);
    const shown = withoutUnseen(first);
    replaceTab(shown);
    history.recordStep(visitOf(shown));
  }

  /** Run `change` as one transition: everything it commits is published once, at the end. */
  function transact<T>(change: () => T): T {
    transactionDepth += 1;
    try {
      return change();
    } finally {
      transactionDepth -= 1;
      if (transactionDepth === 0 && commitPending) {
        commitPending = false;
        commit();
      }
    }
  }

  function markSelected(tabId: TabId) {
    selectedTabId = tabId;
    recentlySelected = [tabId, ...recentlySelected.filter((id) => id !== tabId)];
  }

  function replaceTab(next: WorkspaceTab) {
    tabs = tabs.map((tab) => (tab.id === next.id ? next : tab));
  }

  function planTab(tab: WorkspaceTab): PlanTab {
    return {
      id: tab.id,
      place: tab.place,
      ...(tab.binding ? { binding: tab.binding } : {}),
      ...(tab.role ? { role: tab.role } : {}),
      dirty: isTabDirty(tab.id),
    };
  }

  function resourceStateOfPlace(place: WorkspacePlace): ResourceState {
    if (place.kind === "agent" || (place.kind === "chat" && place.id !== undefined)) {
      return sessionStates.get(place.id!) ?? "unknown";
    }
    if (place.kind !== "terminal") return "live";
    if (endedTerminals.has(place.key)) return "gone";
    if (terminalInventory.status === "loaded" && terminalInventory.keys.includes(place.key)) {
      return "live";
    }
    return "unknown";
  }

  function openState(): OpenState {
    return {
      sessionId: scopeId,
      tabs: knownTabs().map(planTab),
      selectedTabId,
      recentlySelected,
      resourceState: resourceStateOfPlace,
      // Tabs from a newer app count toward the cap too; they're kept, just not shown.
      maxTabs: MAX_WORKSPACE_TABS - (tabs.length - knownTabs().length),
    };
  }

  /** The binding a new tab gets, with the IDs only the store assigns. */
  function newBinding(
    place: WorkspacePlace,
    viewKey?: string,
    draftKey?: string,
  ): TabBinding | undefined {
    const binding = bindingForPlace(place);
    if (!binding) return undefined;
    if (binding.kind === "browser-view")
      return { kind: "browser-view", viewKey: viewKey ?? newViewKey() };
    if (binding.kind === "chat") {
      return binding.id === undefined
        ? { kind: "chat", draftKey: draftKey ?? newDraftKey() }
        : { kind: "chat", id: binding.id, draftKey: binding.id };
    }
    return binding;
  }

  function recordVisit(effect: "append" | "replace" | "none", visit: Visit) {
    if (effect === "append") history.recordStep(visit);
    else if (effect === "replace") history.replaceCurrent(visit);
  }

  function insertAfter(tab: WorkspaceTab, after: TabId | null) {
    const index = after === null ? -1 : indexOf(after);
    const at = index === -1 ? tabs.length : index + 1;
    tabs = [...tabs.slice(0, at), tab, ...tabs.slice(at)];
  }

  type LocalPlan = Exclude<OpenPlan, { action: "other-session" }>;

  function applyPlan(
    plan: LocalPlan,
    opener: TabOpener,
    extras: { viewKey?: string; draftKey?: string },
  ): { tabId: TabId; created: boolean; selected: boolean } {
    switch (plan.action) {
      case "navigate": {
        const tab = findTab(plan.tabId)!;
        const changed = !samePlace(tab.place, plan.place);
        const moved = withPlace(tab, plan.place);
        const next: WorkspaceTab = plan.select
          ? withoutUnseen(moved)
          : { ...moved, ...(changed ? { unseen: true as const } : {}) };
        replaceTab(next);
        land(tab.id);
        if (plan.select) markSelected(tab.id);
        recordVisit(plan.history, changed ? { tabId: tab.id, place: plan.place } : visitOf(next));
        return { tabId: tab.id, created: false, selected: plan.select };
      }
      case "attention": {
        const tab = findTab(plan.tabId)!;
        replaceTab({ ...tab, attention: { place: plan.place } });
        return { tabId: tab.id, created: false, selected: false };
      }
      case "create": {
        const binding = newBinding(plan.place, extras.viewKey, extras.draftKey);
        const tab: WorkspaceTab = {
          id: newTabId(),
          place: plan.place,
          openedBy: opener,
          ...(binding ? { binding } : {}),
          ...(plan.role ? { role: plan.role } : {}),
          ...(plan.select ? {} : { unseen: true as const }),
        };
        // The first tab to arrive in an empty sidebar takes its New tab's place, and shows.
        if (plan.place.kind !== "new") takeEmptyNewTabsPlace();
        insertAfter(tab, plan.after);
        land(tab.id);
        if (plan.select) markSelected(tab.id);
        recordVisit(plan.history, { tabId: tab.id, place: tab.place });
        return { tabId: tab.id, created: true, selected: plan.select };
      }
    }
  }

  /** Run the unsaved-changes guard for `tabIds`, then check nothing moved meanwhile. */
  async function guardThen<T>(
    tabIds: TabId[],
    reason: LeaveReason,
    apply: () => T,
  ): Promise<T | { ok: false; cancelled: true } | { ok: false; stale: true }> {
    const planned = stamp();
    const dirty = tabIds.filter((tabId) => isTabDirty(tabId));
    if (dirty.length > 0) {
      const answer = await askToLeave(dirty, reason);
      if (answer === "cancel") return { ok: false, cancelled: true };
      if (stamp() !== planned) return { ok: false, stale: true };
    }
    return apply();
  }

  /** Ask `tabIds`' editors about their unsaved edits; discarding them isn't a newer action. */
  async function askToLeave(tabIds: TabId[], reason: LeaveReason): Promise<"leave" | "cancel"> {
    for (const tabId of tabIds) asking.add(tabId);
    try {
      return await requestLeave(tabIds, reason);
    } finally {
      for (const tabId of tabIds) asking.delete(tabId);
    }
  }

  /** A tab's editor gained or lost unsaved edits, which every pending guard depends on. */
  function noteDirtyChange(tabId: TabId) {
    revision += 1;
    if (asking.has(tabId)) excusedChanges += 1;
    else onRevision?.();
    snapshot = makeSnapshot();
    for (const listener of [...listeners]) listener();
  }

  /**
   * Open `link`: plan against the current tabs, ask about unsaved edits the plan would
   * replace, and apply only if nothing changed meanwhile. `draftKey` names a side chat
   * draft that already exists, such as "Ask in side chat".
   */
  async function open(
    link: WorkspaceLink,
    openOptions: OpenOptions & { draftKey?: string },
  ): Promise<OpenResult> {
    const { draftKey, ...planOptions } = openOptions;
    const planned = planOpen(openState(), link, planOptions);
    if (!planned.ok) return planned;
    const { plan } = planned;
    if (plan.action === "other-session") return { ok: false, otherSession: plan };
    const run = () => {
      const result = transact(() => {
        const applied = applyPlan(plan, openOptions.opener, { draftKey });
        commit();
        return applied;
      });
      // A tab opened into an empty sidebar is selected even when the plan didn't ask.
      return { ok: true as const, ...result, selected: selectedTabId === result.tabId };
    };
    return guardThen(plan.action === "navigate" ? plan.guard : [], "navigate", run);
  }

  /** Show the browser view `viewKey` in its tab, opening one for it when none is open. */
  function openBrowserView(
    viewKey: string,
    openOptions: { select: boolean; opener: TabOpener },
  ): TabId | null {
    const existing = knownTabs().find(
      (tab) => tab.binding?.kind === "browser-view" && tab.binding.viewKey === viewKey,
    );
    if (existing) {
      if (openOptions.select && existing.id !== selectedTabId) {
        markSelected(existing.id);
        replaceTab(withoutUnseen(existing));
        history.recordStep(visitOf(existing));
        commit();
      }
      return existing.id;
    }
    if (tabs.length >= MAX_WORKSPACE_TABS) return null;
    const tab: WorkspaceTab = {
      id: newTabId(),
      place: { kind: "web" },
      binding: { kind: "browser-view", viewKey },
      openedBy: openOptions.opener,
      ...(openOptions.select ? {} : { unseen: true as const }),
    };
    takeEmptyNewTabsPlace();
    insertAfter(tab, selectedTabId);
    if (openOptions.select) {
      markSelected(tab.id);
      history.recordStep({ tabId: tab.id, place: tab.place });
    }
    commit();
    return tab.id;
  }

  /** Show `place` in `tabId` in place: a step when the tab is selected (or a replacement). */
  async function navigate(
    tabId: TabId,
    place: WorkspacePlace,
    navigateOptions: { replace?: boolean } = {},
  ): Promise<GuardedResult> {
    const tab = findTab(tabId);
    if (!tab) return { ok: false, reason: MISSING_TAB_REASON };
    if (!canShow(planTab(tab), place)) return { ok: false, reason: refusalForTab(tab) };
    if (samePlace(tab.place, place)) return { ok: true };
    const guard = isPositionOnlyMove(tab.place, place) ? [] : [tabId];
    return guardThen(guard, "navigate", () =>
      transact(() => {
        const current = findTab(tabId);
        if (!current) return { ok: false as const, stale: true as const };
        replaceTab(withPlace(current, place));
        land(tabId);
        if (current.id === selectedTabId) {
          const visit = { tabId, place };
          if (navigateOptions.replace || isPositionOnlyMove(current.place, place)) {
            history.replaceCurrent(visit);
          } else {
            history.recordStep(visit);
          }
        }
        commit();
        return { ok: true as const };
      }),
    );
  }

  /** Show `visit` in its browser tab. */
  function landBrowserTab(tab: WorkspaceTab, visit: Visit) {
    replaceTab(showingVisit(withoutUnseen(tab), visit));
  }

  /**
   * Step Back or Forward through the timeline: select the visit's tab and, when it
   * shows something else, navigate it there, asking about unsaved edits first.
   * A browser page lands in the same commit.
   */
  async function go(
    direction: "back" | "forward",
    moveOptions?: HistoryMoveOptions,
  ): Promise<GuardedResult> {
    const stepOptions: HistoryMoveOptions = moveOptions ?? {};
    const target =
      direction === "back" ? history.backTarget(stepOptions) : history.forwardTarget(stepOptions);
    if (!target) return { ok: false, reason: "There's nowhere to go." };
    const tab = findTab(target.tabId);
    if (!tab) return { ok: false, reason: MISSING_TAB_REASON };
    const browser = tab.binding?.kind === "browser-view";
    const moves = !browser && !samePlace(tab.place, target.place);
    return guardThen(moves ? [tab.id] : [], "navigate", () =>
      transact(() => {
        const landed =
          direction === "back" ? history.back(stepOptions) : history.forward(stepOptions);
        const current = landed ? findTab(landed.tabId) : undefined;
        if (!landed || !current) return { ok: false as const, stale: true as const };
        markSelected(current.id);
        land(current.id);
        if (current.binding?.kind === "browser-view") landBrowserTab(current, landed);
        else replaceTab({ ...withoutUnseen(current), ...(moves ? { place: landed.place } : {}) });
        commit();
        return { ok: true as const };
      }),
    );
  }

  /** Select a tab; never asks about unsaved edits, the other tab stays mounted. */
  function select(tabId: TabId): boolean {
    if (tabId === selectedTabId) return true;
    if (!tabs.some((tab) => tab.id === tabId)) return false;
    markSelected(tabId);
    const tab = findTab(tabId);
    if (tab) {
      if (tab.unseen) replaceTab(withoutUnseen(tab));
      history.recordStep(visitOf(tab));
    }
    commit();
    return true;
  }

  /**
   * Move the selection between tabs shown together, such as a split's shells as focus moves
   * between their panes, in place of the current step. Changes nothing and returns false once
   * the selected tab isn't one of `together`, so a selection made meanwhile stands.
   */
  function selectAmong(tabId: TabId, together: readonly TabId[]): boolean {
    if (tabId === selectedTabId) return true;
    if (selectedTabId === null || !together.includes(selectedTabId) || !together.includes(tabId))
      return false;
    const tab = findTab(tabId);
    if (!tab) return false;
    const onSelected = history.state.visits[history.state.cursor]?.tabId === selectedTabId;
    markSelected(tabId);
    const shown = tab.unseen ? withoutUnseen(tab) : tab;
    if (shown !== tab) replaceTab(shown);
    // The cursor is on the tab it leaves unless history lost it; then this is a step after all.
    if (onSelected) history.swapCurrent(visitOf(shown));
    else history.recordStep(visitOf(shown));
    commit();
    return true;
  }

  /** Land after the selected tab went away: the nearest surviving visit, a neighbour, or none. */
  function landAfterRemoval(removedIndex: number, restore: Visit | null) {
    if (restore && tabs.some((tab) => tab.id === restore.tabId)) {
      markSelected(restore.tabId);
      const tab = findTab(restore.tabId);
      if (tab?.binding?.kind === "browser-view") {
        landBrowserTab(tab, restore);
        return;
      }
      if (tab && !samePlace(tab.place, restore.place)) {
        // A dirty tab keeps its buffer; the cursor visit follows what it shows.
        if (isTabDirty(tab.id)) history.replaceCurrent({ tabId: tab.id, place: tab.place });
        else replaceTab({ ...tab, place: restore.place });
      }
      return;
    }
    const neighbour = tabs[Math.min(removedIndex, tabs.length - 1)];
    if (neighbour) {
      markSelected(neighbour.id);
      const tab = findTab(neighbour.id);
      if (tab) history.recordStep(visitOf(tab));
      return;
    }
    // Closing the last tab leaves none for now; a session's commit puts a New tab in its place.
    selectedTabId = null;
  }

  function removeTab(tabId: TabId, reopenable: boolean) {
    const index = indexOf(tabId);
    if (index === -1) return;
    const removed = tabs[index];
    tabs = tabs.filter((tab) => tab.id !== tabId);
    recentlySelected = recentlySelected.filter((id) => id !== tabId);
    if (
      reopenable &&
      !isUnknownTab(removed) &&
      removed.binding?.kind !== "terminal" &&
      removed.place.kind !== "new"
    ) {
      closed = [...closed, { tab: withoutMarks(removed), index }].slice(-MAX_CLOSED_TABS);
    }
    const restore = history.removeTab(tabId);
    if (removed.id === selectedTabId) landAfterRemoval(index, restore);
  }

  /**
   * Close a tab in three phases: ask about unsaved edits, then `confirm` (such as a
   * shell's close dialog), then `teardown` (end the shell, destroy the browser view).
   * Consent is only asked for; nothing is torn down if anything changed meanwhile,
   * so the user's newer action wins and the tab and its resource stay.
   */
  async function close(
    tabId: TabId,
    closeOptions: {
      confirm?: () => Promise<boolean>;
      /** Ends the resource before the tab goes, such as a shell; failure keeps the tab. */
      teardown?: () => Promise<boolean>;
      /** Releases the resource after the tab went, such as a browser view; never blocks it. */
      release?: () => Promise<boolean>;
    } = {},
  ): Promise<GuardedResult> {
    if (!tabs.some((tab) => tab.id === tabId)) return { ok: false, reason: MISSING_TAB_REASON };
    if (isOnlyEmptyNewTab(tabs, tabId)) return { ok: false, reason: LAST_NEW_TAB_REASON };
    const planned = stamp();
    const guarded = await guardThen([tabId], "close", () => ({ ok: true as const }));
    if (!guarded.ok) return guarded;
    if (closeOptions.confirm && !(await closeOptions.confirm())) {
      return { ok: false, cancelled: true };
    }
    if (stamp() !== planned || !tabs.some((tab) => tab.id === tabId)) {
      return { ok: false, stale: true };
    }
    if (closeOptions.teardown && !(await closeOptions.teardown())) {
      return { ok: false, cancelled: true };
    }
    // The resource is gone now, so its tab goes even if something moved during teardown.
    if (!tabs.some((tab) => tab.id === tabId)) return { ok: true };
    transact(() => {
      removeTab(tabId, true);
      commit();
    });
    releaseDetached(closeOptions.release, tabId);
    return { ok: true };
  }

  /**
   * Close a split's shells and every tab of them as one action: one confirm, then each shell
   * ends in turn, and only once it has do its tabs go. A shell that fails to end keeps its tabs
   * and is named in `failed`; anything else changing meanwhile stops the close as stale.
   */
  async function closeTerminalGroup(
    tabIds: readonly TabId[],
    {
      terminalKeys = [],
      confirm,
      teardownFor,
    }: {
      /** Every shell the split names, ended too though it has no tab here. */
      terminalKeys?: readonly string[];
      confirm?: () => Promise<boolean>;
      /** Ends one shell; false when it couldn't. */
      teardownFor: (terminalKey: string) => Promise<boolean>;
    },
  ): Promise<GuardedResult | { ok: false; failed: string[]; stale?: true }> {
    const keyOf = (tabId: TabId) => {
      const binding = findTab(tabId)?.binding;
      return binding?.kind === "terminal" ? binding.key : null;
    };
    const present = tabIds.filter((tabId) => keyOf(tabId) !== null);
    if (present.length === 0) return { ok: false, reason: MISSING_TAB_REASON };
    const keys = [...new Set([...present.map(keyOf), ...terminalKeys])].filter(
      (key): key is string => key !== null,
    );
    if (keys.some((key) => closingTerminals.has(key))) {
      return { ok: false, reason: ALREADY_CLOSING_REASON };
    }
    // Reserved before asking, so a second close of the same shells meanwhile is refused.
    for (const key of keys) closingTerminals.add(key);
    const failed: string[] = [];
    // Stopped as stale, the shells that failed to end are still named: they're still running.
    const stale = () =>
      failed.length > 0 ? { ok: false as const, stale: true as const, failed } : STALE_RESULT;
    try {
      let planned = stamp();
      const guarded = await guardThen(present, "close", () => ({ ok: true as const }));
      if (!guarded.ok) return guarded;
      if (confirm && !(await confirm())) return { ok: false, cancelled: true };
      for (const key of keys) {
        if (stamp() !== planned) return stale();
        // oxlint-disable-next-line no-await-in-loop -- one shell at a time, each checked after
        const ended = await teardownFor(key);
        const changedMeanwhile = stamp() !== planned;
        if (!ended) {
          failed.push(key);
          if (changedMeanwhile) return stale();
          continue;
        }
        // The shell is gone now, so its tabs go even if something else moved meanwhile.
        const closing = tabs.filter(
          (tab) =>
            !isUnknownTab(tab) && tab.binding?.kind === "terminal" && tab.binding.key === key,
        );
        if (closing.length > 0)
          transact(() => {
            for (const tab of closing) removeTab(tab.id, false);
            commit();
          });
        if (changedMeanwhile) return stale();
        planned = stamp();
      }
    } finally {
      for (const key of keys) closingTerminals.delete(key);
    }
    return failed.length > 0 ? { ok: false, failed } : { ok: true };
  }

  /** Tear down a closed tab's resource without holding the store; a failure is only logged. */
  function releaseDetached(release: (() => Promise<boolean>) | undefined, tabId: TabId) {
    if (!release) return;
    void release().then(
      (released) => {
        if (!released)
          console.warn(`Couldn't release the resource of closed workspace tab ${tabId}`);
      },
      (error: unknown) =>
        console.warn(`Couldn't release the resource of closed workspace tab ${tabId}`, error),
    );
  }

  /**
   * Remove `targetIds` in one commit, with history and selection landing there too, so nothing
   * half-closed is ever on screen; `after` runs inside it. Their resources (browser views) are
   * released afterwards, without holding the store; a failed release never brings a tab back.
   */
  function removeBatch(
    targetIds: readonly TabId[],
    releaseFor: (tab: WorkspaceTab) => (() => Promise<boolean>) | undefined,
    after?: () => void,
  ) {
    const releases: { tabId: TabId; release: (() => Promise<boolean>) | undefined }[] = [];
    transact(() => {
      for (const tabId of targetIds) {
        const tab = findTab(tabId);
        if (!tabs.some((candidate) => candidate.id === tabId)) continue;
        releases.push({ tabId, release: tab ? releaseFor(tab) : undefined });
        removeTab(tabId, true);
      }
      after?.();
      commit();
    });
    for (const { tabId, release } of releases) releaseDetached(release, tabId);
  }

  /**
   * Close `tabIds` as one action, such as a group in the tab list: ask about all their unsaved
   * edits first, and Keep editing closes nothing. Pinned tabs and shells stay open; Reopen
   * closed tab brings the others back one at a time.
   */
  async function closeMany(
    tabIds: readonly TabId[],
    releaseFor: (tab: WorkspaceTab) => (() => Promise<boolean>) | undefined,
  ): Promise<GuardedResult> {
    const wanted = new Set(tabIds);
    const targetIds = tabs
      .filter((tab) => wanted.has(tab.id) && isBatchClosable(tab))
      .map((tab) => tab.id);
    if (targetIds.length === 0) return { ok: true };
    const guarded = await guardThen(targetIds, "close", () => ({ ok: true as const }));
    if (!guarded.ok) return guarded;
    removeBatch(targetIds, releaseFor);
    return { ok: true };
  }

  /** Close every tab but `keepTabId`, pinned tabs and shells, as one action, then select it. */
  async function closeOthers(
    keepTabId: TabId,
    releaseFor: (tab: WorkspaceTab) => (() => Promise<boolean>) | undefined,
  ): Promise<GuardedResult> {
    if (!tabs.some((tab) => tab.id === keepTabId)) return { ok: false, reason: MISSING_TAB_REASON };
    const targetIds = tabs
      .filter((tab) => tab.id !== keepTabId && isBatchClosable(tab))
      .map((tab) => tab.id);
    const guarded = await guardThen(targetIds, "close", () => ({ ok: true as const }));
    if (!guarded.ok) return guarded;
    if (!tabs.some((tab) => tab.id === keepTabId)) return { ok: false, stale: true };
    removeBatch(targetIds, releaseFor, () => {
      if (selectedTabId !== keepTabId) {
        markSelected(keepTabId);
        const kept = findTab(keepTabId);
        if (kept) history.recordStep(visitOf(kept));
      }
    });
    return { ok: true };
  }

  /** End every pending guarded action as stale, such as on a session switch. */
  function invalidate() {
    revision += 1;
    onRevision?.();
    snapshot = makeSnapshot();
  }

  const sessionIsLive = (binding: TabBinding) =>
    (binding.kind !== "chat" && binding.kind !== "agent") ||
    binding.id === undefined ||
    sessionStates.get(binding.id) !== "gone";

  /** Why closed entry `entry` can't come back: shells never do, nor a chat that's gone. */
  function closedEntryRefusal(
    entry: ClosedTab,
    isLive: (binding: TabBinding) => boolean = sessionIsLive,
  ): string | null {
    const binding = entry.tab.binding;
    if (binding?.kind === "terminal") return RECENT_GONE_REASON;
    if (binding && binding.kind !== "browser-view" && !isLive(binding)) return RECENT_GONE_REASON;
    return null;
  }

  /** Put closed entry `index` back at its position and select it; the caller checked it can. */
  function reopenClosedEntry(index: number): TabId {
    const entry = closed[index];
    const binding = entry.tab.binding;
    closed = closed.filter((_, position) => position !== index);
    // A new view has none of the old page's history entries.
    const { entry: _staleEntry, ...reopened } = entry.tab;
    const tab: WorkspaceTab = {
      ...reopened,
      id: newTabId(),
      openedBy: "user",
      // Showing a page again is a new view loading the last committed URL.
      ...(binding?.kind === "browser-view"
        ? { binding: { kind: "browser-view" as const, viewKey: newViewKey() } }
        : {}),
    };
    // Only one Files tab navigates in place.
    if (tab.role === "files" && knownTabs().some((existing) => existing.role === "files")) {
      delete tab.role;
    }
    // Reopening after the last tab closed puts the strip back as it was.
    takeEmptyNewTabsPlace();
    const at = Math.min(entry.index, tabs.length);
    tabs = [...tabs.slice(0, at), tab, ...tabs.slice(at)];
    markSelected(tab.id);
    history.recordStep(visitOf(tab));
    commit();
    return tab.id;
  }

  /** Bring back the last closed tab at its position; shells and gone resources are skipped. */
  function reopenClosed(isLive: (binding: TabBinding) => boolean = sessionIsLive): TabId | null {
    if (tabs.length >= MAX_WORKSPACE_TABS) return null;
    for (let index = closed.length - 1; index >= 0; index -= 1) {
      if (closedEntryRefusal(closed[index], isLive) === null) return reopenClosedEntry(index);
    }
    return null;
  }

  /**
   * Places the new-tab page `pageTabId` offers: closed tabs (newest first), then other
   * open tabs (most recently selected first). Only places with an identity, each once,
   * never a shell or a new-tab page; browser pages and draft chats have no identity.
   */
  function recentPlaces(pageTabId: TabId): RecentPlace[] {
    const openTabs = recentTabsOrder(pageTabId);
    const recents: RecentPlace[] = [];
    const seen = new Set<string>();
    const offer = (place: WorkspacePlace) => {
      const identity = placeIdentity(place);
      if (identity === null || seen.has(identity) || recents.length >= MAX_RECENT_PLACES) return;
      seen.add(identity);
      // A place still open somewhere is that tab, title and all, wherever it's listed.
      const openTab = openTabs.find((tab) => placeIdentity(tab.place) === identity);
      recents.push(
        openTab ? { identity, place: openTab.place, openTabId: openTab.id } : { identity, place },
      );
    };
    for (let index = closed.length - 1; index >= 0; index -= 1) {
      const entry = closed[index];
      if (closedEntryRefusal(entry) === null) offer(entry.tab.place);
    }
    for (const tab of openTabs) offer(tab.place);
    return recents;
  }

  /** Open tabs a recent place can go to: most recently selected first, then strip order. */
  function recentTabsOrder(pageTabId: TabId): WorkspaceTab[] {
    const ids = [...recentlySelected, ...knownTabs().map((tab) => tab.id)].filter(
      (tabId, position, all) => all.indexOf(tabId) === position,
    );
    return ids.flatMap((tabId) => {
      const tab = findTab(tabId);
      if (!tab || tab.id === pageTabId || tab.binding?.kind === "terminal") return [];
      return tab.place.kind === "new" ? [] : [tab];
    });
  }

  /**
   * Go to recent place `identity` from the new-tab page `pageTabId`, which gives way to it.
   * An open tab showing it is selected, never duplicated; otherwise its closed tab comes
   * back as Reopen would. The closed entry is used up only when that succeeded.
   */
  function openRecent(pageTabId: TabId, identity: string): { ok: true; tabId: TabId } | Refusal {
    const page = findTab(pageTabId);
    if (!page || page.place.kind !== "new") return { ok: false, reason: MISSING_TAB_REASON };
    // The tab its Recent row stands for, in the order recentPlaces offers them.
    const openTab = recentTabsOrder(pageTabId).find((tab) => placeIdentity(tab.place) === identity);
    const closedIndex = closed.findLastIndex(
      (entry) => placeIdentity(entry.tab.place) === identity,
    );
    if (openTab) {
      return transact(() => {
        // Selected as any tab is (marked seen, a history step) before the page goes, so the
        // page isn't selected when it's removed and nothing else lands.
        if (!select(openTab.id)) return { ok: false as const, reason: MISSING_TAB_REASON };
        removeTab(pageTabId, false);
        // The closed copy would only reopen a duplicate of the open tab, so Reopen skips it.
        if (closedIndex !== -1) closed = closed.filter((_, position) => position !== closedIndex);
        commit();
        return { ok: true as const, tabId: openTab.id };
      });
    }
    if (closedIndex === -1) return { ok: false, reason: RECENT_GONE_REASON };
    const refusal = closedEntryRefusal(closed[closedIndex]);
    if (refusal !== null) return { ok: false, reason: refusal };
    // No tab limit to check: the new-tab page gives way, so the count never grows.
    return transact(() => {
      const tabId = reopenClosedEntry(closedIndex);
      removeTab(pageTabId, false);
      commit();
      return { ok: true as const, tabId };
    });
  }

  function move(tabId: TabId, index: number) {
    const from = indexOf(tabId);
    if (from === -1) return;
    const to = Math.max(0, Math.min(index, tabs.length - 1));
    if (from === to) return;
    const next = tabs.filter((tab) => tab.id !== tabId);
    next.splice(to, 0, tabs[from]);
    // A move across the pinned tabs' edge stops there.
    if (sameOrder(withPinnedFirst(next), tabs)) return;
    tabs = next;
    commit();
  }

  /** Pin `tabIds` to the strip's start, after the pinned tabs, or unpin them to just after. */
  function setPinned(tabIds: readonly TabId[], pinned: boolean): boolean {
    const ids = new Set(tabIds);
    let changed = false;
    tabs = tabs.map((tab) => {
      if (!ids.has(tab.id) || isUnknownTab(tab) || isPinnedTab(tab) === pinned) return tab;
      changed = true;
      if (pinned) return { ...tab, pinned: true as const };
      const { pinned: _pinned, ...rest } = tab;
      return rest;
    });
    if (changed) commit();
    return changed;
  }

  /**
   * Put the tabs in `order`, such as after a drag in the strip. Only a reordering of exactly
   * the open tabs applies; one that a tab opened or closed meanwhile does nothing.
   */
  function reorder(order: readonly TabId[]): boolean {
    if (order.length !== tabs.length || new Set(order).size !== order.length) return false;
    const byId = new Map(tabs.map((tab) => [tab.id, tab]));
    const next = order.map((tabId) => byId.get(tabId));
    if (next.some((tab) => tab === undefined)) return false;
    // A drop across the pinned tabs' edge lands at it, which may leave nothing moved.
    const kept = withPinnedFirst(next as WorkspaceTabRecord[]);
    if (sameOrder(kept, tabs)) return false;
    tabs = kept;
    commit();
    return true;
  }

  function resourceState(tabId: TabId): ResourceState {
    const tab = findTab(tabId);
    const binding = tab?.binding;
    if (!binding || binding.kind === "browser-view") return "live";
    if (binding.kind === "terminal")
      return resourceStateOfPlace({ kind: "terminal", key: binding.key });
    if (binding.kind === "chat" && binding.id === undefined) return "live";
    return sessionStates.get(binding.id!) ?? "unknown";
  }

  /**
   * Take in what a side-chat or sub-agent session's lookup said. Only a definite
   * "gone" (the session doesn't exist) removes its tabs, landing as a close would;
   * a failed lookup keeps them.
   */
  function setSessionState(childSessionId: string, state: ResourceState) {
    if (sessionStates.get(childSessionId) === state) return;
    sessionStates.set(childSessionId, state);
    if (state === "gone") {
      for (const tab of knownTabs()) {
        const binding = tab.binding;
        if (
          (binding?.kind === "chat" || binding?.kind === "agent") &&
          binding.id === childSessionId
        ) {
          removeTab(tab.id, false);
        }
      }
    }
    commit();
  }

  /**
   * Take in the session's shell list. A shell missing from a loaded list has ended
   * if it was seen before or saved from a previous run; its tab goes as if closed.
   * A shell that is still starting stays "Connecting…" until it first appears.
   */
  function setTerminalInventory(inventory: TerminalInventory) {
    // Only a change a shell tab shows counts as a revision, so an unrelated list
    // doesn't overturn an action the user is confirming.
    const shellStates = (except: ReadonlySet<string> = new Set()) =>
      knownTabs()
        .filter((tab) => !(tab.binding?.kind === "terminal" && except.has(tab.binding.key)))
        .map((tab) => (tab.binding?.kind === "terminal" ? resourceState(tab.id) : ""))
        .join();
    const before = shellStates();
    const othersBefore = shellStates(closingTerminals);
    let removed = false;
    let removedOther = false;
    terminalInventory = inventory;
    if (inventory.status === "loaded") {
      for (const key of inventory.keys) {
        seenTerminals.add(key);
        endedTerminals.delete(key);
      }
      for (const tab of knownTabs()) {
        const binding = tab.binding;
        if (binding?.kind !== "terminal" || inventory.keys.includes(binding.key)) continue;
        if (seenTerminals.has(binding.key)) {
          endedTerminals.add(binding.key);
          removeTab(tab.id, false);
          removed = true;
          if (!closingTerminals.has(binding.key)) removedOther = true;
        }
      }
    }
    if (!removed && shellStates() === before) return;
    commit();
    // Only the shells a group close is ending went, and nothing else changed: that close stays
    // current.
    const othersChanged = shellStates(closingTerminals) !== othersBefore;
    if (removed && !removedOther && !othersChanged && closingTerminals.size > 0)
      excusedChanges += 1;
  }

  /** The first send created the side chat: fill in its ID, once. */
  function completeDraftChat(tabId: TabId, chatId: string): boolean {
    const tab = findTab(tabId);
    if (tab?.binding?.kind !== "chat" || tab.binding.id !== undefined) return false;
    const next: WorkspaceTab = {
      ...tab,
      binding: { ...tab.binding, id: chatId },
      place: { kind: "chat", id: chatId },
    };
    replaceTab(next);
    history.replaceNewTab(tabId, { tabId, place: next.place });
    commit();
    return true;
  }

  /** Show what an agent opened in the tab: a user navigation, through the guard. */
  async function showAttention(tabId: TabId): Promise<GuardedResult> {
    const tab = findTab(tabId);
    if (!tab?.attention) return { ok: false, reason: MISSING_TAB_REASON };
    const place = tab.attention.place;
    const guard = isPositionOnlyMove(tab.place, place) ? [] : [tabId];
    return guardThen(guard, "navigate", () =>
      transact(() => {
        const current = findTab(tabId);
        if (!current?.attention) return { ok: false as const, stale: true as const };
        const wasSelected = current.id === selectedTabId;
        const shown = withPlace(withoutMarks(current), place);
        replaceTab(shown);
        land(tabId);
        markSelected(tabId);
        recordVisit(
          wasSelected && isPositionOnlyMove(current.place, place) ? "replace" : "append",
          samePlace(current.place, place) ? visitOf(shown) : { tabId, place },
        );
        commit();
        return { ok: true as const };
      }),
    );
  }

  function dismissAttention(tabId: TabId) {
    const tab = findTab(tabId);
    if (!tab?.attention) return;
    const { attention: _attention, ...rest } = tab;
    replaceTab(rest);
    commit();
  }

  /**
   * Turn the new-tab page `tabId` into an app. An unbound app shows in the same tab;
   * a bound one (a shell, browser or side chat) takes the tab's slot as a new tab.
   * Single-instance apps select their open tab and drop the empty one. Either way
   * Back never returns to the new-tab page.
   */
  function fillNewTab(tabId: TabId, place: WorkspacePlace, viewKey?: string): TabId | null {
    const tab = findTab(tabId);
    if (!tab || tab.place.kind !== "new") return null;
    const binding = newBinding(place, viewKey);
    const identity = binding ? null : placeIdentity(place);
    const existing = knownTabs().find((candidate) => {
      if (candidate.id === tabId) return false;
      // A shell, agent or chat that a tab already shows, such as one the shell list added
      // while its launch was still answering. A blank browser page is always a new one.
      if (binding) return binding.kind !== "browser-view" && canShow(planTab(candidate), place);
      if (identity === null || candidate.binding) return false;
      return identity === "files"
        ? candidate.role === "files"
        : placeIdentity(candidate.place) === identity;
    });
    if (existing) {
      return transact(() => {
        // Selected first, as openRecent does, so removing the New tab lands nowhere else.
        if (!select(existing.id)) return null;
        removeTab(tabId, false);
        commit();
        return existing.id;
      });
    }
    const filesTab = knownTabs().some((candidate) => candidate.role === "files");
    const next: WorkspaceTab = {
      id: binding ? newTabId() : tabId,
      place,
      openedBy: "user",
      ...(binding ? { binding } : {}),
      // A pinned New tab stays pinned as whatever it turns into.
      ...(isPinnedTab(tab) ? { pinned: true as const } : {}),
      ...(place.kind === "files" && place.path === undefined && !filesTab
        ? { role: "files" as const }
        : {}),
    };
    tabs = tabs.map((candidate) => (candidate.id === tabId ? next : candidate));
    if (selectedTabId === tabId) markSelected(next.id);
    recentlySelected = recentlySelected.filter((id) => id !== tabId || next.id === tabId);
    history.replaceNewTab(tabId, { tabId: next.id, place });
    commit();
    return next.id;
  }

  return {
    scopeId,
    tabs: () => snapshot.tabs,
    selectedTabId: () => snapshot.selectedTabId,
    selectedTab: () => (selectedTabId === null ? undefined : findTab(selectedTabId)),
    findTab,
    /** How `tabId` last landed on its visit, if it did since the store was made. */
    landing: (tabId: TabId): TabLanding | undefined => landings.get(tabId),
    revision: stamp,
    snapshot: () => snapshot,
    serialize,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    planOpen: (link: WorkspaceLink, planOptions: OpenOptions) =>
      planOpen(openState(), link, planOptions),
    open,
    openBrowserView,
    navigate,
    /** Whether `tabId` is selected right now, even inside a transition not yet published. */
    isSelected: (tabId: TabId) => selectedTabId === tabId,
    select,
    selectAmong,
    go,
    close,
    closeTerminalGroup,
    closeOthers,
    closeMany,
    setPinned,
    invalidate,
    noteDirtyChange,
    askToLeave,
    setSessionState: (childSessionId: string, state: ResourceState) =>
      transact(() => setSessionState(childSessionId, state)),
    reopenClosed,
    recentPlaces,
    openRecent,
    move,
    reorder,
    resourceState,
    shellWaits,
    setTerminalInventory: (inventory: TerminalInventory) =>
      transact(() => setTerminalInventory(inventory)),
    completeDraftChat,
    showAttention,
    dismissAttention,
    fillNewTab: (tabId: TabId, place: WorkspacePlace, viewKey?: string) =>
      transact(() => fillNewTab(tabId, place, viewKey)),
    history,
  };
}
