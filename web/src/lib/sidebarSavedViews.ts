// The sidebar's saved views: a name for a grouping, ordering, row details and filters, kept on
// this device for each server and user. A stored view this build can't read is kept as it is,
// so an older or newer build never loses another's views.

import { useCallback, useMemo, useSyncExternalStore } from "react";

import { randomUUID } from "@/lib/randomUUID";

import { DEFAULT_SESSION_FILTER, type SessionFilter } from "./sessionFilterPreferences";
import {
  DEFAULT_SESSION_LIST_FILTER,
  parseSessionListFilter,
  type SessionListFilter,
  type SessionProjectFilter,
  type SessionPullRequestFilter,
  type SessionStatusFilter,
} from "./sessionListFilters";
import {
  DEFAULT_SIDEBAR_VIEW,
  SIDEBAR_GROUPINGS,
  SIDEBAR_SHOW_FIELDS,
  type SidebarGrouping,
  type SidebarShowField,
} from "./sidebarViewPreferences";

const STORAGE_KEY = "omnigent:sidebar-saved-views";
/** The most views one user keeps on a server; past it a new view is refused, never an old one dropped. */
export const SAVED_VIEW_LIMIT = 20;
/** The longest name a view takes. */
export const SAVED_VIEW_NAME_LIMIT = 40;

/** The orderings a view saves; Custom is a hand-placed order, so it isn't one. */
export type SavedViewOrdering = "updated" | "created";
const SAVED_VIEW_ORDERINGS: readonly SavedViewOrdering[] = ["updated", "created"];
const SESSION_FILTERS: readonly SessionFilter[] = ["all", "mine", "shared", "archived"];

/** What a view applies to the sidebar. */
export interface SavedViewSettings {
  grouping: SidebarGrouping;
  ordering: SavedViewOrdering;
  show: readonly SidebarShowField[];
  sessionFilter: SessionFilter;
  pullRequestFilter: SessionPullRequestFilter;
  statusFilter: SessionStatusFilter;
  /** A harness key; null keeps every harness. One no longer offered is kept as stored. */
  harnessFilter: string | null;
  /** By project id, so a renamed project still matches; one since deleted is kept as stored. */
  projectFilter: SessionProjectFilter;
}

/** A view's filters, as the sidebar's filters take them. */
export function savedViewListFilter(view: SavedViewSettings): SessionListFilter {
  return {
    pullRequest: view.pullRequestFilter,
    status: view.statusFilter,
    harness: view.harnessFilter,
    project: view.projectFilter,
  };
}

/** The sidebar's filters, as a view stores them. */
export function savedViewFilterFields(
  filter: SessionListFilter,
): Pick<
  SavedViewSettings,
  "pullRequestFilter" | "statusFilter" | "harnessFilter" | "projectFilter"
> {
  return {
    pullRequestFilter: filter.pullRequest,
    statusFilter: filter.status,
    harnessFilter: filter.harness,
    projectFilter: filter.project,
  };
}

/** No filter on: what a view saved before the filters existed reads as. */
export const DEFAULT_SAVED_VIEW_FILTERS = savedViewFilterFields(DEFAULT_SESSION_LIST_FILTER);

export interface SidebarSavedView extends SavedViewSettings {
  id: string;
  name: string;
}

/** Whose views: the server the page talks to, and its signed-in user. */
export interface SavedViewScope {
  server: string;
  userId: string;
}

/** Why a view wasn't saved. */
export type SaveViewProblem = "empty" | "duplicate" | "limit" | "storage";
export type SaveViewResult =
  { ok: true; view: SidebarSavedView } | { ok: false; problem: SaveViewProblem };

const scopeKey = (scope: SavedViewScope) => `${scope.server}\n${scope.userId}`;

function pick<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.find((choice) => choice === value) ?? fallback;
}

/** A stored view, each setting falling back on its own; null without an id and a name. */
export function parseSavedView(value: unknown): SidebarSavedView | null {
  if (typeof value !== "object" || value === null) return null;
  const stored = value as Record<string, unknown>;
  const { id, name } = stored;
  if (typeof id !== "string" || id === "" || typeof name !== "string" || name.trim() === "")
    return null;
  const show = Array.isArray(stored.show) ? stored.show : [];
  // Each filter falls back to All on its own, so a view saved before a filter existed reads as it was.
  const filter = parseSessionListFilter({
    pullRequest: stored.pullRequestFilter,
    status: stored.statusFilter,
    harness: stored.harnessFilter,
    project: stored.projectFilter,
  });
  return {
    id,
    name: name.trim().slice(0, SAVED_VIEW_NAME_LIMIT),
    grouping: pick(SIDEBAR_GROUPINGS, stored.grouping, DEFAULT_SIDEBAR_VIEW.grouping),
    ordering: pick(SAVED_VIEW_ORDERINGS, stored.ordering, "updated"),
    // The menu's order, so equal selections compare equal.
    show: SIDEBAR_SHOW_FIELDS.filter((field) => show.includes(field)),
    sessionFilter: pick(SESSION_FILTERS, stored.sessionFilter, DEFAULT_SESSION_FILTER),
    ...savedViewFilterFields(filter),
  };
}

/**
 * Every scope's stored value, as stored, or null when storage can't be read or holds something
 * this build doesn't recognize: then nothing is changed, so no view is lost.
 */
function readAll(): Record<string, unknown> | null {
  if (typeof window === "undefined") return null;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (raw === null) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** `scope`'s entries as stored, readable or not; null when they can't be read. */
function storedEntries(scope: SavedViewScope): unknown[] | null {
  const all = readAll();
  if (all === null) return null;
  const entries = all[scopeKey(scope)];
  if (entries === undefined) return [];
  return Array.isArray(entries) ? entries : null;
}

const listeners = new Set<() => void>();
function notify() {
  for (const listener of listeners) listener();
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    // A null key is another window clearing storage.
    if (event.key === STORAGE_KEY || event.key === null) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** Replaces `scope`'s entries, keeping every other scope's as stored; false when it can't. */
function writeScope(scope: SavedViewScope, entries: unknown[]): boolean {
  const all = readAll();
  if (all === null) return false;
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...all, [scopeKey(scope)]: entries }),
    );
  } catch {
    return false;
  }
  notify();
  return true;
}

/** The views `scope` keeps, in the order they were saved. */
export function readSavedViews(scope: SavedViewScope): SidebarSavedView[] {
  return (storedEntries(scope) ?? []).flatMap((entry) => parseSavedView(entry) ?? []);
}

const sameName = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/** Why `name` can't join `entries`: taken, or the limit reached. Entries this build can't read count too. */
function addProblem(entries: readonly unknown[], name: string): "duplicate" | "limit" | null {
  const views = entries.flatMap((entry) => parseSavedView(entry) ?? []);
  if (views.some((view) => sameName(view.name, name))) return "duplicate";
  if (entries.length >= SAVED_VIEW_LIMIT) return "limit";
  return null;
}

/** Only the settings a view keeps, so a view passed as settings lends neither its id nor its name. */
function savedViewSettings(settings: SavedViewSettings): SavedViewSettings {
  const { grouping, ordering, show, sessionFilter } = settings;
  return {
    grouping,
    ordering,
    show,
    sessionFilter,
    ...savedViewFilterFields(savedViewListFilter(settings)),
  };
}

/** Saves `settings` as a new view named `name`, or says why it can't. */
export function saveView(
  scope: SavedViewScope,
  name: string,
  settings: SavedViewSettings,
): SaveViewResult {
  const trimmed = name.trim().slice(0, SAVED_VIEW_NAME_LIMIT);
  if (trimmed === "") return { ok: false, problem: "empty" };
  const entries = storedEntries(scope);
  if (entries === null) return { ok: false, problem: "storage" };
  const problem = addProblem(entries, trimmed);
  if (problem) return { ok: false, problem };
  const view: SidebarSavedView = {
    ...savedViewSettings(settings),
    id: randomUUID(),
    name: trimmed,
  };
  return writeScope(scope, [...entries, view])
    ? { ok: true, view }
    : { ok: false, problem: "storage" };
}

/** Why a deleted view didn't come back; it's still gone, and nothing else changed. */
export type RestoreViewResult =
  { ok: true } | { ok: false; problem: Exclude<SaveViewProblem, "empty"> };

/**
 * Deletes the view `id`, returning a call that puts it back, or null when it's gone or storage
 * refuses the change. Putting it back follows the same rules as saving, and places it before the
 * nearest view that followed it, or after the nearest that preceded it, among those still kept.
 */
export function deleteView(scope: SavedViewScope, id: string): (() => RestoreViewResult) | null {
  const entries = storedEntries(scope);
  if (entries === null) return null;
  const index = entries.findIndex((entry) => parseSavedView(entry)?.id === id);
  if (index === -1) return null;
  const removed = entries[index];
  const removedView = parseSavedView(removed);
  if (!removedView) return null;
  // Its neighbours as stored, nearest first, matched again by their stored form.
  const following = entries.slice(index + 1).map((entry) => JSON.stringify(entry));
  const preceding = entries
    .slice(0, index)
    .reverse()
    .map((entry) => JSON.stringify(entry));
  if (
    !writeScope(
      scope,
      entries.filter((_, at) => at !== index),
    )
  )
    return null;
  return () => {
    const now = storedEntries(scope);
    if (now === null) return { ok: false, problem: "storage" };
    if (now.some((entry) => parseSavedView(entry)?.id === id)) return { ok: true };
    const problem = addProblem(now, removedView.name);
    if (problem) return { ok: false, problem };
    const stored = now.map((entry) => JSON.stringify(entry));
    const next = following.map((entry) => stored.indexOf(entry)).find((at) => at !== -1);
    const previous = preceding.map((entry) => stored.indexOf(entry)).find((at) => at !== -1);
    const at = next ?? (previous === undefined ? now.length : previous + 1);
    return writeScope(scope, [...now.slice(0, at), removed, ...now.slice(at)])
      ? { ok: true }
      : { ok: false, problem: "storage" };
  };
}

const sameProjectFilter = (a: SessionProjectFilter, b: SessionProjectFilter) =>
  a.kind === b.kind && (a.kind !== "project" || (b.kind === "project" && a.id === b.id));

/** The saved view the sidebar shows now, if any: the first whose every setting matches. */
export function matchingSavedView(
  current: {
    grouping: SidebarGrouping;
    ordering: string;
    show: readonly SidebarShowField[];
  } & Pick<
    SavedViewSettings,
    "sessionFilter" | "pullRequestFilter" | "statusFilter" | "harnessFilter" | "projectFilter"
  >,
  views: readonly SidebarSavedView[],
): SidebarSavedView | null {
  return (
    views.find(
      (view) =>
        view.grouping === current.grouping &&
        view.ordering === current.ordering &&
        view.sessionFilter === current.sessionFilter &&
        view.pullRequestFilter === current.pullRequestFilter &&
        view.statusFilter === current.statusFilter &&
        view.harnessFilter === current.harnessFilter &&
        sameProjectFilter(view.projectFilter, current.projectFilter) &&
        view.show.length === current.show.length &&
        view.show.every((field) => current.show.includes(field)),
    ) ?? null
  );
}

/** `scope`'s views, live as they're saved or deleted here or in another window. */
export function useSidebarSavedViews(scope: SavedViewScope | null): SidebarSavedView[] {
  const snapshot = useCallback(
    () => (scope ? JSON.stringify(storedEntries(scope) ?? []) : "[]"),
    [scope],
  );
  const stored = useSyncExternalStore(subscribe, snapshot, () => "[]");
  return useMemo(() => {
    const entries: unknown[] = JSON.parse(stored);
    return entries.flatMap((entry) => parseSavedView(entry) ?? []);
  }, [stored]);
}
