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

let current = DEFAULT_SIDEBAR_VIEW;
let persistedRaw: string | null | undefined;
let unsaved = false;
const listeners = new Set<() => void>();

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

/** Stable snapshot; a failed save stays authoritative until a later write succeeds. */
export function readSidebarViewPreferences(): SidebarViewPreferences {
  if (typeof window === "undefined") return DEFAULT_SIDEBAR_VIEW;
  if (!unsaved) {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw !== persistedRaw) {
        persistedRaw = raw;
        current = parseSidebarViewPreferences(raw);
      }
    } catch {
      // Keep the current session usable when browser storage is unavailable.
    }
  }
  return current;
}

/** Publish first; persistence is best-effort and must never roll back in-memory changes. */
export function writeSidebarViewPreferences(value: SidebarViewPreferences): void {
  if (typeof window === "undefined") return;
  current = value;
  unsaved = true;
  try {
    const raw = JSON.stringify(value);
    window.localStorage.setItem(STORAGE_KEY, raw);
    persistedRaw = raw;
    unsaved = false;
  } catch {
    // The next write retries persistence using the current in-memory view.
  }
  for (const listener of listeners) listener();
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
