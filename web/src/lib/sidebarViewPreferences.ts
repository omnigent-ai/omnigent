// Device-local sidebar grouping, ordering, and row-metadata preferences.

const STORAGE_KEY = "omnigent:sidebar-view";

export type SidebarGrouping = "default" | "status" | "harness" | "created" | "updated";
export type SidebarOrdering = "updated" | "created" | "custom";
export type SidebarShowField = "updated" | "environment" | "repo" | "branch";

/** Saved positions on top of the automatic order Custom started from. */
export interface SidebarCustomOrder {
  base: "updated" | "created";
  /** Newest server timestamp when Custom started: later-created rows are new. */
  since: number;
  /** Rows the user placed (or saw when Custom started), top to bottom. */
  ids: readonly string[];
}

export interface SidebarViewPreferences {
  grouping: SidebarGrouping;
  ordering: SidebarOrdering;
  show: readonly SidebarShowField[];
  /** Kept while another ordering is selected, so Custom can be restored. */
  custom?: SidebarCustomOrder;
}

export const SIDEBAR_GROUPINGS: readonly SidebarGrouping[] = [
  "default",
  "status",
  "harness",
  "created",
  "updated",
];
export const SIDEBAR_ORDERINGS: readonly SidebarOrdering[] = ["updated", "created", "custom"];
export const SIDEBAR_SHOW_FIELDS: readonly SidebarShowField[] = [
  "updated",
  "environment",
  "repo",
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
  const custom = parseCustomOrder(stored.custom);
  const ordering = pick(SIDEBAR_ORDERINGS, stored.ordering, DEFAULT_SIDEBAR_VIEW.ordering);
  return {
    grouping: pick(SIDEBAR_GROUPINGS, stored.grouping, DEFAULT_SIDEBAR_VIEW.grouping),
    ordering: ordering === "custom" && !custom ? DEFAULT_SIDEBAR_VIEW.ordering : ordering,
    // Keep the menu's field order so equal selections compare equal.
    show: SIDEBAR_SHOW_FIELDS.filter((field) => show.includes(field)),
    ...(custom && { custom }),
  };
}

function parseCustomOrder(value: unknown): SidebarCustomOrder | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const { base, since, ids } = value as Record<string, unknown>;
  if ((base !== "updated" && base !== "created") || !Number.isFinite(since) || !Array.isArray(ids))
    return undefined;
  return {
    base,
    since: since as number,
    ids: [...new Set(ids.filter((id): id is string => typeof id === "string"))],
  };
}

/** The server sort behind the selected ordering; Custom sorts its unsaved rows by its base. */
export function sidebarBaseOrdering(view: SidebarViewPreferences): "updated" | "created" {
  return view.ordering === "custom" ? (view.custom?.base ?? "updated") : view.ordering;
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
