// Persisted, per-device preference for which slice the sidebar's session list
// shows ("All sessions" / "My sessions" / "Shared sessions" / "Archived
// sessions").
//
// The sidebar keeps its live React state as the source of truth; these helpers
// only seed that state on mount and snapshot it when the visible tab changes,
// so a reload lands on the slice the viewer was last looking at instead of
// snapping back to "All sessions". It's a device-local view preference — no
// account or session state is changed — so it lives in localStorage like the
// other `*Preferences` helpers.

const STORAGE_KEY = "omnigent:session-filter";

/**
 * Which slice of sessions the sidebar shows. Mirrors the sidebar's tab
 * vocabulary; kept here so validation and the stored format live together.
 */
export type SessionFilter = "all" | "mine" | "shared" | "archived";

export const DEFAULT_SESSION_FILTER: SessionFilter = "all";

const SESSION_FILTERS = new Set<string>(["all", "mine", "shared", "archived"]);

/**
 * Read the persisted session filter. Returns {@link DEFAULT_SESSION_FILTER}
 * when nothing is stored, on a server render (no `window`), when storage is
 * inaccessible, or when the stored value isn't a filter this build knows —
 * never throws, so a corrupt or stale entry can't strand the viewer on a slice
 * the menu can't show.
 *
 * `multiUser` mirrors the sidebar's own gate: a loopback-only server has one
 * user, so "Shared sessions" is dropped from the menu there. A "shared" value
 * stored against a multi-user server therefore falls back to the default
 * rather than scoping the list to a slice with no menu entry to leave it by.
 */
export function readSessionFilter(multiUser: boolean): SessionFilter {
  if (typeof window === "undefined") return DEFAULT_SESSION_FILTER;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return DEFAULT_SESSION_FILTER;
  }
  if (raw === null || !SESSION_FILTERS.has(raw)) return DEFAULT_SESSION_FILTER;
  if (raw === "shared" && !multiUser) return DEFAULT_SESSION_FILTER;
  return raw as SessionFilter;
}

/**
 * Persist the session filter. Swallows quota/access errors so a failed write
 * can't break the sidebar.
 */
export function writeSessionFilter(value: SessionFilter): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // The filter is a local view preference; losing it is harmless.
  }
}
