// One session's back/forward timeline across workspace tabs: visits in order and
// a cursor. Pure: the tab store decides what is a step, a replacement or a close,
// runs the leave guard, and keeps the cursor on the selected tab's place.

import { samePlace } from "./planOpen";
import type { BrowserEntry, TabId, Visit, WorkspacePlace } from "./workspacePlace";

/** How many visits a timeline keeps; the oldest fall off. */
export const WORKSPACE_HISTORY_LIMIT = 100;

export interface WorkspaceHistoryState {
  readonly visits: readonly Visit[];
  /** Index of the current visit; -1 only when there are no visits. */
  readonly cursor: number;
}

/** Lets the caller disable Back/Forward onto some visits, such as a page still replaying. */
export interface HistoryMoveOptions {
  isBlocked?: (visit: Visit) => boolean;
}

export const EMPTY_WORKSPACE_HISTORY: WorkspaceHistoryState = { visits: [], cursor: -1 };

/** A fresh timeline holding only `visit`. */
export function historyStartingAt(visit: Visit): WorkspaceHistoryState {
  return { visits: [visit], cursor: 0 };
}

export function currentVisit(state: WorkspaceHistoryState): Visit | null {
  return state.visits[state.cursor] ?? null;
}

function entriesEqual(left: BrowserEntry | undefined, right: BrowserEntry | undefined): boolean {
  return left?.index === right?.index;
}

/** Same tab, same place and same page entry. */
export function visitsEqual(left: Visit, right: Visit): boolean {
  return (
    left.tabId === right.tabId &&
    samePlace(left.place, right.place) &&
    entriesEqual(left.entry, right.entry)
  );
}

/**
 * Append `visit` after the cursor, dropping Forward. Recording the visit the
 * cursor is already on changes nothing, so a repeated selection isn't a step.
 */
export function recordStep(
  state: WorkspaceHistoryState,
  visit: Visit,
  limit = WORKSPACE_HISTORY_LIMIT,
): WorkspaceHistoryState {
  const current = currentVisit(state);
  if (current && visitsEqual(current, visit)) return state;
  const visits = [...state.visits.slice(0, state.cursor + 1), visit];
  const overflow = Math.max(0, visits.length - limit);
  return { visits: visits.slice(overflow), cursor: visits.length - 1 - overflow };
}

/**
 * Replace the cursor visit (a line move, a redirect). Only a visit of the same
 * tab is replaced; the same move into another tab is a tab switch, so it appends.
 */
export function replaceCurrent(
  state: WorkspaceHistoryState,
  visit: Visit,
  limit = WORKSPACE_HISTORY_LIMIT,
): WorkspaceHistoryState {
  const current = currentVisit(state);
  if (!current || current.tabId !== visit.tabId) return recordStep(state, visit, limit);
  if (visitsEqual(current, visit)) return state;
  const visits = state.visits.slice();
  visits[state.cursor] = visit;
  return collapseRepeats({ visits, cursor: state.cursor });
}

/**
 * Put `visit` at the cursor in place of whatever tab's visit is there, keeping Back and Forward:
 * a move between tabs shown together (a split's shells) that isn't a step of its own.
 */
export function swapCurrent(state: WorkspaceHistoryState, visit: Visit): WorkspaceHistoryState {
  const current = currentVisit(state);
  if (!current) return historyStartingAt(visit);
  if (visitsEqual(current, visit)) return state;
  const visits = state.visits.slice();
  visits[state.cursor] = visit;
  return collapseRepeats({ visits, cursor: state.cursor });
}

/**
 * The new-tab page became an app: `oldTabId`'s visit is replaced by `visit`, in
 * the same tab (an unbound app) or a bound tab created in its slot. It is not a
 * step, so the new-tab page is never a place Back returns to.
 */
export function replaceNewTab(
  state: WorkspaceHistoryState,
  oldTabId: TabId,
  visit: Visit,
  limit = WORKSPACE_HISTORY_LIMIT,
): WorkspaceHistoryState {
  const current = currentVisit(state);
  const slot =
    current?.tabId === oldTabId
      ? state.cursor
      : state.visits.findLastIndex((v) => v.tabId === oldTabId);
  if (slot < 0) return recordStep(state, visit, limit);
  const visits: Visit[] = [];
  let cursor = -1;
  state.visits.forEach((existing, index) => {
    if (index === slot) visits.push(visit);
    else if (existing.tabId !== oldTabId) visits.push(existing);
    else return;
    if (index <= state.cursor) cursor = visits.length - 1;
  });
  return collapseRepeats({ visits, cursor });
}

/**
 * Remove a closed or ended tab's visits. When the cursor was on that tab it
 * moves to the nearest earlier surviving visit, else the nearest later one, and
 * that visit is returned for the caller to restore as Back would. Null when the
 * cursor was elsewhere or nothing survives (the caller opens a new-tab page then).
 */
export function removeTab(
  state: WorkspaceHistoryState,
  tabId: TabId,
): { state: WorkspaceHistoryState; restore: Visit | null } {
  if (!state.visits.some((visit) => visit.tabId === tabId)) return { state, restore: null };
  const cursorWasOnTab = currentVisit(state)?.tabId === tabId;
  const visits: Visit[] = [];
  let cursor = -1;
  state.visits.forEach((visit, index) => {
    if (visit.tabId === tabId) return;
    visits.push(visit);
    if (index <= state.cursor) cursor = visits.length - 1;
  });
  if (cursor < 0 && visits.length > 0) cursor = 0;
  const next = collapseRepeats({ visits, cursor });
  return { state: next, restore: cursorWasOnTab ? currentVisit(next) : null };
}

function stepTarget(
  state: WorkspaceHistoryState,
  direction: -1 | 1,
  options: HistoryMoveOptions,
): number | null {
  const index = state.cursor + direction;
  const visit = state.visits[index];
  if (!visit || options.isBlocked?.(visit)) return null;
  return index;
}

/** The visit Back would land on, without moving the cursor. */
export function backTarget(
  state: WorkspaceHistoryState,
  options: HistoryMoveOptions = {},
): Visit | null {
  const index = stepTarget(state, -1, options);
  return index === null ? null : state.visits[index]!;
}

/** The visit Forward would land on, without moving the cursor. */
export function forwardTarget(
  state: WorkspaceHistoryState,
  options: HistoryMoveOptions = {},
): Visit | null {
  const index = stepTarget(state, 1, options);
  return index === null ? null : state.visits[index]!;
}

/** Move the cursor one visit back; unchanged when Back isn't possible. */
export function goBack(
  state: WorkspaceHistoryState,
  options: HistoryMoveOptions = {},
): WorkspaceHistoryState {
  const index = stepTarget(state, -1, options);
  return index === null ? state : { ...state, cursor: index };
}

/** Move the cursor one visit forward; unchanged when Forward isn't possible. */
export function goForward(
  state: WorkspaceHistoryState,
  options: HistoryMoveOptions = {},
): WorkspaceHistoryState {
  const index = stepTarget(state, 1, options);
  return index === null ? state : { ...state, cursor: index };
}

/** Put the cursor on an existing visit, keeping the timeline (a page's own ‹ ›). */
export function moveCursorTo(state: WorkspaceHistoryState, index: number): WorkspaceHistoryState {
  if (index < 0 || index >= state.visits.length || index === state.cursor) return state;
  return { ...state, cursor: index };
}

/**
 * Restore the invariant for the selected tab: nothing when the cursor visit
 * already equals it, a replacement when the cursor is on that tab, else a step.
 */
export function alignWithSelection(
  state: WorkspaceHistoryState,
  visit: Visit,
  limit = WORKSPACE_HISTORY_LIMIT,
): WorkspaceHistoryState {
  return currentVisit(state)?.tabId === visit.tabId
    ? replaceCurrent(state, visit, limit)
    : recordStep(state, visit, limit);
}

/** The cursor visit belongs to `tabId` and shows `place`. */
export function cursorMatches(
  state: WorkspaceHistoryState,
  tabId: TabId,
  place: WorkspacePlace,
): boolean {
  const current = currentVisit(state);
  return current !== null && current.tabId === tabId && samePlace(current.place, place);
}

/** Plain JSON for the session's persisted workspace state. */
export function serializeHistory(state: WorkspaceHistoryState): {
  visits: Visit[];
  cursor: number;
} {
  return { visits: state.visits.map((visit) => structuredClone(visit)), cursor: state.cursor };
}

/**
 * Read persisted history, keeping only well-formed visits of tabs that still
 * exist (`isKnownTab`). The cursor follows its visit, else the nearest earlier
 * survivor. Place contents are the persisted tab state's to validate.
 */
export function restoreHistory(
  raw: unknown,
  isKnownTab: (tabId: TabId) => boolean,
  limit = WORKSPACE_HISTORY_LIMIT,
): WorkspaceHistoryState {
  if (!isRecord(raw) || !Array.isArray(raw.visits)) return EMPTY_WORKSPACE_HISTORY;
  const rawCursor =
    typeof raw.cursor === "number" && Number.isInteger(raw.cursor) ? raw.cursor : -1;
  const visits: Visit[] = [];
  let cursor = -1;
  raw.visits.forEach((candidate: unknown, index: number) => {
    const visit = readVisit(candidate);
    if (!visit || !isKnownTab(visit.tabId)) return;
    visits.push(visit);
    if (index <= rawCursor) cursor = visits.length - 1;
  });
  const overflow = Math.max(0, visits.length - limit);
  const kept = visits.slice(overflow);
  if (kept.length === 0) return EMPTY_WORKSPACE_HISTORY;
  return collapseRepeats({
    visits: kept,
    cursor: Math.min(Math.max(cursor - overflow, 0), kept.length - 1),
  });
}

function readVisit(candidate: unknown): Visit | null {
  if (!isRecord(candidate) || typeof candidate.tabId !== "string" || candidate.tabId === "")
    return null;
  const place = candidate.place;
  if (!isRecord(place) || typeof place.kind !== "string") return null;
  const visit: Visit = { tabId: candidate.tabId, place: place as unknown as WorkspacePlace };
  const entry = candidate.entry;
  if (
    isRecord(entry) &&
    typeof entry.index === "number" &&
    Number.isInteger(entry.index) &&
    entry.index >= 0
  ) {
    visit.entry = { index: entry.index };
  }
  return visit;
}

// A removal or replacement can leave two identical visits side by side
// ([F:a, C:x, F:a] after closing C); Back over them would do nothing.
function collapseRepeats(state: WorkspaceHistoryState): WorkspaceHistoryState {
  const visits: Visit[] = [];
  let cursor = -1;
  state.visits.forEach((visit, index) => {
    const previous = visits.at(-1);
    if (!previous || !visitsEqual(previous, visit)) visits.push(visit);
    if (index === state.cursor) cursor = visits.length - 1;
  });
  return { visits, cursor };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * A session's timeline as an object with the store-facing method names. The
 * tab store owns one per session and snapshots `state` for its revision checks.
 */
export class WorkspaceHistory {
  private current: WorkspaceHistoryState;

  constructor(initial: WorkspaceHistoryState = EMPTY_WORKSPACE_HISTORY) {
    this.current = initial;
  }

  get state(): WorkspaceHistoryState {
    return this.current;
  }

  set state(next: WorkspaceHistoryState) {
    this.current = next;
  }

  recordStep(visit: Visit): void {
    this.current = recordStep(this.current, visit);
  }

  replaceCurrent(visit: Visit): void {
    this.current = replaceCurrent(this.current, visit);
  }

  swapCurrent(visit: Visit): void {
    this.current = swapCurrent(this.current, visit);
  }

  replaceNewTab(oldTabId: TabId, visit: Visit): void {
    this.current = replaceNewTab(this.current, oldTabId, visit);
  }

  removeTab(tabId: TabId): Visit | null {
    const result = removeTab(this.current, tabId);
    this.current = result.state;
    return result.restore;
  }

  backTarget(options?: HistoryMoveOptions): Visit | null {
    return backTarget(this.current, options);
  }

  forwardTarget(options?: HistoryMoveOptions): Visit | null {
    return forwardTarget(this.current, options);
  }

  back(options?: HistoryMoveOptions): Visit | null {
    const target = backTarget(this.current, options);
    this.current = goBack(this.current, options);
    return target;
  }

  forward(options?: HistoryMoveOptions): Visit | null {
    const target = forwardTarget(this.current, options);
    this.current = goForward(this.current, options);
    return target;
  }

  canGoBack(options?: HistoryMoveOptions): boolean {
    return backTarget(this.current, options) !== null;
  }

  canGoForward(options?: HistoryMoveOptions): boolean {
    return forwardTarget(this.current, options) !== null;
  }

  serialize(): { visits: Visit[]; cursor: number } {
    return serializeHistory(this.current);
  }

  restore(raw: unknown, isKnownTab: (tabId: TabId) => boolean): void {
    this.current = restoreHistory(raw, isKnownTab);
  }
}
