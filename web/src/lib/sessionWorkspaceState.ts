// Per-session UI state for the right "Workspace" rail, keyed by conversationId
// so each session restores its own layout: whether the rail is open, its width,
// the selected rail tab, and the set of open file tabs (plus which one is
// active). A brand-new session (no stored entry) starts closed at the default
// width with no open files.

import type { RightRailTab } from "@/shell/railTabs";

const RAIL_TABS: readonly RightRailTab[] = ["files", "subagents", "terminals", "todos", "browser"];

export interface SessionWorkspaceState {
  /** Whether the rail was left open in this session. */
  open?: boolean;
  /** User-chosen rail width (px) for this session. */
  widthPx?: number;
  /** The selected rail tab (Files / Agents / Shells / Tasks). */
  rightRailTab?: RightRailTab;
  /** Ordered list of open file tabs. */
  openFiles?: string[];
  /** The active file tab (null = a scope view is active). */
  selectedFilePath?: string | null;
}

const STORAGE_KEY = "omnigent:session-workspace-state";
// Cap stored sessions so the store can't grow without bound. The
// least-recently-touched entries (front of the array) are pruned first once the
// cap is exceeded.
const MAX_SESSIONS = 100;
// Cap the open-file tabs persisted per session so one session that churns
// through many files can't bloat the store. Tabs are appended in open order, so
// the most-recent (tail) entries are kept.
const MAX_OPEN_FILES = 20;

function isValidWidth(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function isRailTab(value: unknown): value is RightRailTab {
  return typeof value === "string" && (RAIL_TABS as readonly string[]).includes(value);
}

/**
 * One persisted session entry. The store is an ordered array (not a keyed
 * object) so recency ordering survives serialization regardless of the id
 * format: a plain `Record` reorders integer-like keys (e.g. numeric
 * conversation ids) into ascending numeric order, which would break both the
 * "touch = move to end" refresh and the oldest-first pruning.
 */
interface StoredEntry {
  id: string;
  state: SessionWorkspaceState;
}

type Store = StoredEntry[];

function sanitize(entry: unknown): SessionWorkspaceState {
  if (typeof entry !== "object" || entry === null) return {};
  const record = entry as Record<string, unknown>;
  const state: SessionWorkspaceState = {};
  if (typeof record.open === "boolean") state.open = record.open;
  if (isValidWidth(record.widthPx)) state.widthPx = record.widthPx;
  if (isRailTab(record.rightRailTab)) state.rightRailTab = record.rightRailTab;
  if (Array.isArray(record.openFiles) && record.openFiles.every((p) => typeof p === "string")) {
    state.openFiles = record.openFiles as string[];
  }
  if (record.selectedFilePath === null || typeof record.selectedFilePath === "string") {
    state.selectedFilePath = record.selectedFilePath;
  }
  return state;
}

function readStore(): Store {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const store: Store = [];
    for (const item of parsed) {
      if (typeof item !== "object" || item === null) continue;
      const record = item as Record<string, unknown>;
      if (typeof record.id !== "string") continue;
      store.push({ id: record.id, state: sanitize(record.state) });
    }
    return store;
  } catch {
    return [];
  }
}

function writeStore(store: Store): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // Storage quota/access errors must not break panel interactions.
  }
}

/** Read one session's persisted workspace state (absent/invalid fields omitted). */
export function readSessionWorkspaceState(conversationId: string): SessionWorkspaceState {
  const entry = readStore().find((e) => e.id === conversationId);
  return entry ? entry.state : {};
}

/** Merge a partial update into one session's persisted workspace state. */
export function writeSessionWorkspaceState(
  conversationId: string,
  patch: SessionWorkspaceState,
): void {
  const store = readStore();
  const existingIdx = store.findIndex((e) => e.id === conversationId);
  const prev = existingIdx >= 0 ? store[existingIdx].state : {};
  const next = { ...prev, ...patch };
  // Keep only the most-recent open-file tabs (tabs are appended in open order).
  if (next.openFiles && next.openFiles.length > MAX_OPEN_FILES) {
    next.openFiles = next.openFiles.slice(-MAX_OPEN_FILES);
  }
  // Drop any existing entry and re-append so the most-recently-touched session
  // moves to the end; pruning then evicts from the front (oldest-touched).
  if (existingIdx >= 0) store.splice(existingIdx, 1);
  store.push({ id: conversationId, state: next });
  if (store.length > MAX_SESSIONS) {
    store.splice(0, store.length - MAX_SESSIONS);
  }
  writeStore(store);
}
