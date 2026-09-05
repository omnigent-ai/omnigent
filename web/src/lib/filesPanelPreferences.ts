// Persisted, app-global preference for the Working folder file list.
//
// Holds the changed-files sort order. It's a *preference*, not per-conversation
// state: the user's choice "carries over" as they switch in and out of sessions
// and should survive a page refresh, mirroring the global-toggle semantics of
// fileViewPreferences. It's stored under a single localStorage key with no
// per-conversation keying.
//
// AppShell keeps the live React state as the source of truth for the UI; these
// helpers only seed that state on mount and snapshot it when the user changes
// the sort, so a refresh (or a brand-new conversation) starts from the last
// choice instead of the hardcoded default.

import { type ChangedSort, isValidSort } from "@/lib/changedSort";

export interface FilesPanelPreferences {
  /** Sort order for the changed-files flat list. */
  sort: ChangedSort;
}

const STORAGE_KEY = "omnigent:files-panel-preferences";

export const DEFAULT_FILES_PANEL_PREFERENCES: FilesPanelPreferences = {
  sort: "recent",
};

/**
 * Read the persisted Files-panel preference. Returns the default when nothing
 * is stored, on a server render (no `window`), or when the stored value is
 * malformed — never throws, so a corrupt entry can't break the app.
 */
export function readFilesPanelPreferences(): FilesPanelPreferences {
  if (typeof window === "undefined") return DEFAULT_FILES_PANEL_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_FILES_PANEL_PREFERENCES;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return DEFAULT_FILES_PANEL_PREFERENCES;
    }
    const p = parsed as Record<string, unknown>;
    return {
      sort:
        typeof p.sort === "string" && isValidSort(p.sort)
          ? p.sort
          : DEFAULT_FILES_PANEL_PREFERENCES.sort,
    };
  } catch {
    return DEFAULT_FILES_PANEL_PREFERENCES;
  }
}

/**
 * Persist the Files-panel preference. Swallows quota/access errors so a failed
 * write can't break the panel.
 */
export function writeFilesPanelPreferences(prefs: FilesPanelPreferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
