// Device-local sidebar grouping, ordering, and row-metadata preferences.

const STORAGE_KEY = "omnigent:sidebar-view";

export type SidebarGrouping = "default" | "status" | "harness" | "created" | "updated";
export type SidebarOrdering = "updated" | "created" | "custom";
export type SidebarShowField = "updated" | "environment" | "repo" | "branch";

export interface SidebarCustomOrder {
  baseOrdering: Exclude<SidebarOrdering, "custom">;
  ids: readonly string[];
  startedAt: number;
  // ponytail: unmoved arrivals stay in this tab; full-history snapshots would need IndexedDB.
  capturedIds?: readonly string[];
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

let current: SidebarViewPreferences | undefined;
let persisted: SidebarViewPreferences | undefined;
const listeners = new Set<() => void>();

export function resetSidebarViewPreferencesForTests(): void {
  current = undefined;
  persisted = undefined;
}

export function subscribeSidebarViewPreferences(listener: () => void) {
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

/** Normalize persisted fields independently so an invalid option cannot strand the sidebar. */
function parseSidebarViewPreferences(raw: string | null): SidebarViewPreferences {
  let stored: Partial<Record<keyof SidebarViewPreferences, unknown>>;
  try {
    const parsed: unknown = JSON.parse(raw ?? "null");
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

/** Each tab owns its snapshot; another tab's preferences take effect only on reload. */
export function readSidebarViewPreferences(): SidebarViewPreferences {
  if (typeof window === "undefined") return DEFAULT_SIDEBAR_VIEW;
  if (!current) {
    try {
      current = parseSidebarViewPreferences(window.localStorage.getItem(STORAGE_KEY));
      persisted = current;
    } catch {
      current = DEFAULT_SIDEBAR_VIEW;
    }
  }
  return current;
}

/** Publish first; persistence is best-effort and must never roll back in-memory changes. */
export function writeSidebarViewPreferences(value: SidebarViewPreferences): void {
  if (typeof window === "undefined") return;
  current = value;
  const order = value.customOrder;
  if (
    !persisted ||
    persisted.grouping !== value.grouping ||
    persisted.ordering !== value.ordering ||
    persisted.show !== value.show ||
    persisted.customOrder?.ids !== order?.ids ||
    persisted.customOrder?.baseOrdering !== order?.baseOrdering ||
    persisted.customOrder?.startedAt !== order?.startedAt
  ) {
    try {
      const customOrder = order && {
        baseOrdering: order.baseOrdering,
        ids: order.ids,
        startedAt: order.startedAt,
      };
      const snapshot = { ...value, customOrder };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
      persisted = snapshot;
    } catch {
      // The next write retries persistence without rolling back the in-memory view.
    }
  }
  for (const listener of listeners) listener();
}

/** Forget confirmed deletions or lost access, never merely unloaded sessions. */
export function removeSidebarCustomOrderIds(removedIds: readonly string[]): void {
  const view = readSidebarViewPreferences();
  if (!view.customOrder || removedIds.length === 0) return;
  const removed = new Set(removedIds);
  const ids = view.customOrder.ids.filter((id) => !removed.has(id));
  const capturedIds = view.customOrder.capturedIds?.filter((id) => !removed.has(id));
  const savedChanged = ids.length !== view.customOrder.ids.length;
  if (savedChanged || capturedIds?.length !== view.customOrder.capturedIds?.length) {
    writeSidebarViewPreferences({
      ...view,
      customOrder: {
        ...view.customOrder,
        ids: savedChanged ? ids : view.customOrder.ids,
        ...(capturedIds && { capturedIds }),
      },
    });
  }
}
