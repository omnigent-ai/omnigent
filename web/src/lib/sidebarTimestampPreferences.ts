// Persisted, per-device preference for relative session timestamps in the
// conversations sidebar. Session-state badges keep using the same trailing
// slot regardless of this preference; only idle-session times are optional.

export const SIDEBAR_TIMESTAMPS_STORAGE_KEY = "omnigent:show-sidebar-timestamps";
const CHANGE_EVENT = "omnigent:sidebar-timestamps-change";

export const DEFAULT_SHOW_SIDEBAR_TIMESTAMPS = true;

const listeners = new Set<() => void>();

function notifyListeners(): void {
  for (const listener of listeners) listener();
}

function onStorageChange(event: StorageEvent): void {
  if (event.key === SIDEBAR_TIMESTAMPS_STORAGE_KEY || event.key === null) notifyListeners();
}

/** Read whether idle-session timestamps should appear in the sidebar. */
export function readShowSidebarTimestamps(): boolean {
  if (typeof window === "undefined") return DEFAULT_SHOW_SIDEBAR_TIMESTAMPS;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_TIMESTAMPS_STORAGE_KEY);
    if (raw === null) return DEFAULT_SHOW_SIDEBAR_TIMESTAMPS;
    if (raw === "true") return true;
    if (raw === "false") return false;
    return DEFAULT_SHOW_SIDEBAR_TIMESTAMPS;
  } catch {
    return DEFAULT_SHOW_SIDEBAR_TIMESTAMPS;
  }
}

/** Persist the preference and notify mounted same-tab consumers immediately. */
export function writeShowSidebarTimestamps(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (value === DEFAULT_SHOW_SIDEBAR_TIMESTAMPS) {
      window.localStorage.removeItem(SIDEBAR_TIMESTAMPS_STORAGE_KEY);
    } else {
      window.localStorage.setItem(SIDEBAR_TIMESTAMPS_STORAGE_KEY, "false");
    }
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}

/** Subscribe to same-tab writes and cross-tab storage changes. */
export function subscribeShowSidebarTimestamps(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};

  if (listeners.size === 0) {
    window.addEventListener(CHANGE_EVENT, notifyListeners);
    window.addEventListener("storage", onStorageChange);
  }
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) {
      window.removeEventListener(CHANGE_EVENT, notifyListeners);
      window.removeEventListener("storage", onStorageChange);
    }
  };
}
