// Per-device preference for where a sub-agent's chat opens. Off (the default)
// navigates to the sub-agent's own page, replacing the parent chat; on opens
// it as a tab in the parent's Workspace panel so the parent stays visible.

export const OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY = "omnigent:open-subagents-in-panel";

export const DEFAULT_OPEN_SUBAGENTS_IN_PANEL = false;

/** Read the preference; malformed or unreadable storage yields the default. */
export function readOpenSubagentsInPanel(): boolean {
  if (typeof window === "undefined") return DEFAULT_OPEN_SUBAGENTS_IN_PANEL;
  try {
    return window.localStorage.getItem(OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY) === "on";
  } catch {
    return DEFAULT_OPEN_SUBAGENTS_IN_PANEL;
  }
}

/** Persist the preference, clearing the key for the default; false if storage refused it. */
export function writeOpenSubagentsInPanel(enabled: boolean): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (enabled) {
      window.localStorage.setItem(OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY, "on");
    } else {
      window.localStorage.removeItem(OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY);
    }
    return true;
  } catch {
    // localStorage failures should not make Settings unusable.
    return false;
  }
}
