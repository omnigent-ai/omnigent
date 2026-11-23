// Saved workspace tabs, one entry per session under `omnigent:workspace-tabs`.
// Reads are forgiving: malformed fields are dropped one by one, tabs from a newer
// app are kept as they are, and a store written by a newer version is never
// overwritten.

import { type ComparisonChoice, isWorkingCopyBaseIntent } from "@/lib/comparisonChoice";

import type {
  ExplicitRange,
  LineRange,
  PullRequestView,
  TabId,
  Visit,
  WorkspacePlace,
} from "./workspacePlace";
import type {
  ClosedTab,
  TabBinding,
  TabOpener,
  WorkspaceTab,
  WorkspaceTabRecord,
} from "./workspaceTab";

export const WORKSPACE_TABS_STORAGE_KEY = "omnigent:workspace-tabs";
export const WORKSPACE_TABS_VERSION = 1;
/** Sessions kept; the least recently saved go first. */
export const MAX_SAVED_SESSIONS = 100;
/** Tabs per session, above the classic layout's maximum of 4 + 6 × 20 = 124. */
export const MAX_WORKSPACE_TABS = 200;
export const MAX_CLOSED_TABS = 20;
export const MAX_TIMELINE_VISITS = 100;

export interface WorkspaceTabsSessionState {
  tabs: WorkspaceTabRecord[];
  selectedTabId: TabId | null;
  timeline: Visit[];
  cursor: number;
  closed: ClosedTab[];
}

interface StoredSession {
  id: string;
  state: WorkspaceTabsSessionState;
}

interface WorkspaceTabsStoreSnapshot {
  /** False when a newer version wrote the store; this app must not overwrite it. */
  writable: boolean;
  sessions: StoredSession[];
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value !== "";
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

function parseLine(value: unknown): LineRange | undefined {
  if (!isRecord(value) || !positiveInteger(value.start)) return undefined;
  const line: LineRange = { start: value.start };
  if (positiveInteger(value.end) && value.end >= value.start) line.end = value.end;
  if (positiveInteger(value.column)) line.column = value.column;
  return line;
}

function parseRange(value: unknown): ExplicitRange | undefined {
  if (!isRecord(value) || !nonEmptyString(value.base)) return undefined;
  return nonEmptyString(value.head) ? { base: value.base, head: value.head } : { base: value.base };
}

function parseComparison(value: unknown): ComparisonChoice | undefined {
  if (!isRecord(value)) return undefined;
  if (value.kind === "saved") {
    return typeof value.intent === "string" && isWorkingCopyBaseIntent(value.intent)
      ? { kind: "saved", intent: value.intent }
      : undefined;
  }
  if (value.kind !== "typed" || !nonEmptyString(value.base)) return undefined;
  return {
    kind: "typed",
    base: value.base,
    head: typeof value.head === "string" ? value.head : "",
  };
}

const PULL_REQUEST_VIEWS: readonly PullRequestView[] = [
  "summary",
  "comments",
  "commits",
  "changes",
];

/** The place in `value`, null for a malformed one, or "unknown" for a kind this app lacks. */
export function parsePlace(value: unknown): WorkspacePlace | null | "unknown" {
  if (!isRecord(value) || typeof value.kind !== "string") return null;
  const optional = <K extends string, V>(key: K, parsed: V | undefined) =>
    (parsed === undefined ? {} : { [key]: parsed }) as Partial<Record<K, V>>;
  const path = nonEmptyString(value.path) ? value.path : undefined;
  const comment = nonEmptyString(value.comment) ? value.comment : undefined;
  switch (value.kind) {
    case "new":
      return value.app === "terminal" ? { kind: "new", app: "terminal" } : { kind: "new" };
    case "files":
      return {
        kind: "files",
        ...optional("path", path),
        ...(path === undefined ? {} : optional("line", parseLine(value.line))),
        ...(path === undefined ? {} : optional("comment", comment)),
      };
    case "changes":
      return {
        kind: "changes",
        ...optional("path", path),
        ...(path === undefined ? {} : optional("line", parseLine(value.line))),
        ...(path === undefined ? {} : optional("comment", comment)),
        ...optional("range", parseRange(value.range)),
        ...optional("comparison", parseComparison(value.comparison)),
        ...(path !== undefined && value.fileTab === true ? { fileTab: true as const } : {}),
      };
    case "pulls":
      return { kind: "pulls" };
    case "pull": {
      if (!nonEmptyString(value.url)) return null;
      const view = PULL_REQUEST_VIEWS.find((candidate) => candidate === value.view) ?? "summary";
      return {
        kind: "pull",
        url: value.url,
        view,
        ...(value.focus === "checks" ? { focus: "checks" as const } : {}),
        ...optional("commit", nonEmptyString(value.commit) ? value.commit : undefined),
        ...optional("path", path),
        ...(path === undefined ? {} : optional("line", parseLine(value.line))),
      };
    }
    case "terminal":
      return nonEmptyString(value.key) ? { kind: "terminal", key: value.key } : null;
    case "agents":
      return { kind: "agents" };
    case "tasks":
      return { kind: "tasks" };
    case "agent":
      return nonEmptyString(value.id) ? { kind: "agent", id: value.id } : null;
    case "chat":
      return nonEmptyString(value.id) ? { kind: "chat", id: value.id } : { kind: "chat" };
    case "search":
      return nonEmptyString(value.query)
        ? { kind: "search", query: value.query }
        : { kind: "search" };
    case "web":
      return nonEmptyString(value.url) ? { kind: "web", url: value.url } : { kind: "web" };
    default:
      return "unknown";
  }
}

function parseBinding(value: unknown): TabBinding | null | "unknown" {
  if (!isRecord(value) || typeof value.kind !== "string") return null;
  switch (value.kind) {
    case "browser-view":
      return nonEmptyString(value.viewKey)
        ? { kind: "browser-view", viewKey: value.viewKey }
        : null;
    case "terminal":
      return nonEmptyString(value.key) ? { kind: "terminal", key: value.key } : null;
    case "agent":
      return nonEmptyString(value.id) ? { kind: "agent", id: value.id } : null;
    case "chat":
      if (!nonEmptyString(value.draftKey)) return null;
      return nonEmptyString(value.id)
        ? { kind: "chat", id: value.id, draftKey: value.draftKey }
        : { kind: "chat", draftKey: value.draftKey };
    default:
      return "unknown";
  }
}

const OPENERS: readonly TabOpener[] = ["user", "link", "agent", "restore"];

/** A saved tab, kept opaque when a newer app wrote a kind this one lacks; null drops it. */
export function parseTab(value: unknown): WorkspaceTabRecord | null {
  if (!isRecord(value) || !nonEmptyString(value.id)) return null;
  const place = parsePlace(value.place);
  const binding = value.binding === undefined ? undefined : parseBinding(value.binding);
  if (place === "unknown" || binding === "unknown") return { id: value.id, unknown: value };
  if (place === null || binding === null) return null;
  const tab: WorkspaceTab = {
    id: value.id,
    place,
    openedBy: OPENERS.find((opener) => opener === value.openedBy) ?? "restore",
  };
  if (binding) tab.binding = binding;
  if (value.role === "files" && !binding) tab.role = "files";
  if (value.unseen === true) tab.unseen = true;
  if (value.pinned === true) tab.pinned = true;
  if (isRecord(value.entry) && Number.isInteger(value.entry.index)) {
    tab.entry = { index: value.entry.index as number };
  }
  if (isRecord(value.attention)) {
    const pending = parsePlace(value.attention.place);
    if (pending !== null && pending !== "unknown") tab.attention = { place: pending };
  }
  return tab;
}

function parseVisit(value: unknown, tabIds: ReadonlySet<string>): Visit | null {
  if (!isRecord(value) || !nonEmptyString(value.tabId) || !tabIds.has(value.tabId)) return null;
  const place = parsePlace(value.place);
  if (place === null || place === "unknown") return null;
  const visit: Visit = { tabId: value.tabId, place };
  if (isRecord(value.entry) && Number.isInteger(value.entry.index)) {
    visit.entry = { index: value.entry.index as number };
  }
  return visit;
}

/** One session's saved state with malformed fields dropped; null when nothing usable remains. */
export function parseSessionState(value: unknown): WorkspaceTabsSessionState | null {
  if (!isRecord(value) || !Array.isArray(value.tabs)) return null;
  const tabs: WorkspaceTabRecord[] = [];
  const seen = new Set<string>();
  for (const raw of value.tabs.slice(0, MAX_WORKSPACE_TABS)) {
    const tab = parseTab(raw);
    if (tab && !seen.has(tab.id)) {
      seen.add(tab.id);
      tabs.push(tab);
    }
  }
  const savedCursor =
    typeof value.cursor === "number" && Number.isInteger(value.cursor) ? value.cursor : null;
  // The cursor follows its own visit as dropped and trimmed visits leave the timeline.
  const kept: Visit[] = [];
  let keptThroughCursor = 0;
  if (Array.isArray(value.timeline)) {
    value.timeline.forEach((raw, index) => {
      const visit = parseVisit(raw, seen);
      if (visit === null) return;
      kept.push(visit);
      if (savedCursor !== null && index <= savedCursor) keptThroughCursor += 1;
    });
  }
  const trimmed = Math.max(0, kept.length - MAX_TIMELINE_VISITS);
  const timeline = kept.slice(trimmed);
  const cursor =
    savedCursor === null
      ? timeline.length - 1
      : Math.min(Math.max(keptThroughCursor - 1 - trimmed, -1), timeline.length - 1);
  const closed: ClosedTab[] = [];
  if (Array.isArray(value.closed)) {
    for (const raw of value.closed.slice(-MAX_CLOSED_TABS)) {
      if (!isRecord(raw) || !Number.isInteger(raw.index)) continue;
      const tab = parseTab(raw.tab);
      if (tab && !("unknown" in tab)) closed.push({ tab, index: raw.index as number });
    }
  }
  const state: WorkspaceTabsSessionState = {
    tabs,
    selectedTabId:
      nonEmptyString(value.selectedTabId) && seen.has(value.selectedTabId)
        ? value.selectedTabId
        : null,
    timeline,
    cursor,
    closed,
  };
  return state;
}

function readSnapshot(): WorkspaceTabsStoreSnapshot {
  if (typeof window === "undefined") return { writable: false, sessions: [] };
  let parsed: unknown;
  try {
    const raw = window.localStorage.getItem(WORKSPACE_TABS_STORAGE_KEY);
    if (!raw) return { writable: true, sessions: [] };
    parsed = JSON.parse(raw);
  } catch {
    return { writable: true, sessions: [] };
  }
  if (!isRecord(parsed)) return { writable: true, sessions: [] };
  if (typeof parsed.version === "number" && parsed.version > WORKSPACE_TABS_VERSION) {
    return { writable: false, sessions: [] };
  }
  const sessions: StoredSession[] = [];
  if (Array.isArray(parsed.sessions)) {
    for (const entry of parsed.sessions) {
      if (!isRecord(entry) || !nonEmptyString(entry.id)) continue;
      const state = parseSessionState(entry);
      if (state) sessions.push({ id: entry.id, state });
    }
  }
  return { writable: true, sessions };
}

export type ReadSessionResult =
  | { status: "saved"; state: WorkspaceTabsSessionState }
  | { status: "missing" }
  /** A newer app owns the store: render from a fresh migration and don't save. */
  | { status: "read-only" };

export function readWorkspaceTabsSession(sessionId: string): ReadSessionResult {
  const snapshot = readSnapshot();
  if (!snapshot.writable) return { status: "read-only" };
  const entry = snapshot.sessions.find((session) => session.id === sessionId);
  return entry ? { status: "saved", state: entry.state } : { status: "missing" };
}

/** One sidebar's state as it is saved; `parseSessionState` reads it back. */
export function serializeSessionState(state: WorkspaceTabsSessionState): Record<string, unknown> {
  return {
    // An opaque tab is saved exactly as it was read.
    tabs: state.tabs.map((tab) => ("unknown" in tab ? tab.unknown : tab)),
    selectedTabId: state.selectedTabId,
    timeline: state.timeline,
    cursor: state.cursor,
    closed: state.closed,
  };
}

function serializeSession(id: string, state: WorkspaceTabsSessionState): UnknownRecord {
  return { id, ...serializeSessionState(state) };
}

export type SaveSessionResult = "saved" | "failed" | "read-only";

/** Save one session, moving it to the most recent end; old sessions past the cap go. */
export function saveWorkspaceTabsSession(
  sessionId: string,
  state: WorkspaceTabsSessionState,
): SaveSessionResult {
  if (typeof window === "undefined") return "failed";
  const snapshot = readSnapshot();
  if (!snapshot.writable) return "read-only";
  const sessions = snapshot.sessions
    .filter((session) => session.id !== sessionId)
    .map((session) => serializeSession(session.id, session.state));
  sessions.push(serializeSession(sessionId, state));
  const kept = sessions.slice(-MAX_SAVED_SESSIONS);
  try {
    window.localStorage.setItem(
      WORKSPACE_TABS_STORAGE_KEY,
      JSON.stringify({ version: WORKSPACE_TABS_VERSION, sessions: kept }),
    );
    return "saved";
  } catch {
    return "failed";
  }
}
