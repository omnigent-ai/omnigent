// Persisted, per-device sidebar view options from the Sessions filter menu:
// how the session list is grouped and ordered, and which per-session metadata
// rows show. Like `sessionFilterPreferences`, it's a device-local view
// preference — no account or session state changes — so it lives in
// localStorage and only seeds the sidebar's React state on mount.

const STORAGE_KEY = "omnigent:sidebar-view";

export type SidebarGrouping = "default" | "status" | "updated";
export type SidebarOrdering = "updated" | "status";
export type SidebarShowField = "updated" | "environment" | "branch";

export interface SidebarViewPreferences {
  grouping: SidebarGrouping;
  ordering: SidebarOrdering;
  show: readonly SidebarShowField[];
}

export const SIDEBAR_GROUPINGS: readonly SidebarGrouping[] = ["default", "status", "updated"];
export const SIDEBAR_ORDERINGS: readonly SidebarOrdering[] = ["updated", "status"];
export const SIDEBAR_SHOW_FIELDS: readonly SidebarShowField[] = [
  "updated",
  "environment",
  "branch",
];

export const DEFAULT_SIDEBAR_VIEW: SidebarViewPreferences = {
  grouping: "default",
  ordering: "updated",
  show: [],
};

function pick<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

/**
 * Read the persisted view options. Each field falls back to its default on its
 * own, so a stale or hand-edited entry can't strand the viewer on an option the
 * menu no longer offers. Never throws.
 */
export function readSidebarViewPreferences(): SidebarViewPreferences {
  if (typeof window === "undefined") return DEFAULT_SIDEBAR_VIEW;
  let stored: Partial<Record<keyof SidebarViewPreferences, unknown>>;
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    if (typeof parsed !== "object" || parsed === null) return DEFAULT_SIDEBAR_VIEW;
    stored = parsed;
  } catch {
    return DEFAULT_SIDEBAR_VIEW;
  }
  const show = Array.isArray(stored.show) ? stored.show : [];
  return {
    grouping: pick(SIDEBAR_GROUPINGS, stored.grouping, DEFAULT_SIDEBAR_VIEW.grouping),
    ordering: pick(SIDEBAR_ORDERINGS, stored.ordering, DEFAULT_SIDEBAR_VIEW.ordering),
    // Keep the menu's field order so equal selections compare equal.
    show: SIDEBAR_SHOW_FIELDS.filter((field) => show.includes(field)),
  };
}

/** Persist the view options. Swallows quota/access errors. */
export function writeSidebarViewPreferences(value: SidebarViewPreferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // A local view preference; losing it is harmless.
  }
}
