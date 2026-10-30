// Device-local sidebar grouping, ordering, and row-metadata preferences.

const STORAGE_KEY = "omnigent:sidebar-view";

export type SidebarGrouping = "default" | "status" | "harness" | "created" | "updated";
export type SidebarOrdering = "updated" | "created" | "custom";
export type SidebarShowField = "updated" | "environment" | "repo" | "branch";

export interface SidebarCustomOrder {
  baseOrdering: Exclude<SidebarOrdering, "custom">;
  ids: readonly string[];
  startedAt: number;
}

export interface SidebarViewPreferences {
  grouping: SidebarGrouping;
  ordering: SidebarOrdering;
  show: readonly SidebarShowField[];
  customOrder?: SidebarCustomOrder;
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

const listeners = new Set<(view: SidebarViewPreferences) => void>();

export function subscribeSidebarViewPreferences(listener: (view: SidebarViewPreferences) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function pick<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

export function sidebarBaseOrdering(
  view: SidebarViewPreferences,
): SidebarCustomOrder["baseOrdering"] {
  return view.ordering === "custom" ? (view.customOrder?.baseOrdering ?? "updated") : view.ordering;
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
  const custom = stored.customOrder as Partial<SidebarCustomOrder> | null | undefined;
  const customOrder: SidebarCustomOrder | undefined =
    custom &&
    Array.isArray(custom.ids) &&
    typeof custom.startedAt === "number" &&
    Number.isFinite(custom.startedAt) &&
    custom.startedAt >= 0
      ? {
          baseOrdering: pick(["updated", "created"], custom.baseOrdering, "updated"),
          ids: [...new Set(custom.ids.filter((id) => typeof id === "string" && id.length > 0))],
          startedAt: custom.startedAt,
        }
      : undefined;
  const ordering = pick(SIDEBAR_ORDERINGS, stored.ordering, DEFAULT_SIDEBAR_VIEW.ordering);
  return {
    grouping: pick(SIDEBAR_GROUPINGS, stored.grouping, DEFAULT_SIDEBAR_VIEW.grouping),
    ordering: ordering === "custom" && !customOrder ? "updated" : ordering,
    // Keep the menu's field order so equal selections compare equal.
    show: SIDEBAR_SHOW_FIELDS.filter((field) => show.includes(field)),
    ...(customOrder && { customOrder }),
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
  for (const listener of listeners) listener(value);
}

/** Forget confirmed deletions or lost access, never merely unloaded sessions. */
export function removeSidebarCustomOrderIds(removedIds: readonly string[]): void {
  const view = readSidebarViewPreferences();
  if (!view.customOrder || removedIds.length === 0) return;
  const removed = new Set(removedIds);
  const ids = view.customOrder.ids.filter((id) => !removed.has(id));
  if (ids.length !== view.customOrder.ids.length) {
    writeSidebarViewPreferences({ ...view, customOrder: { ...view.customOrder, ids } });
  }
}
