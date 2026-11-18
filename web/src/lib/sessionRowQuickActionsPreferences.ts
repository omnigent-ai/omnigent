// Per-device choice of whether sidebar session rows show their hover Pin and
// Archive buttons beside the "…" menu. Off keeps both actions in the menus only.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const SESSION_ROW_QUICK_ACTIONS_STORAGE_KEY = "omnigent:session-row-quick-actions";

export const DEFAULT_SESSION_ROW_QUICK_ACTIONS = true;

const preference = createStoredChoicePreference(
  SESSION_ROW_QUICK_ACTIONS_STORAGE_KEY,
  ["false", "true"],
  DEFAULT_SESSION_ROW_QUICK_ACTIONS ? "true" : "false",
);

/** Read the preference; storage errors and unknown values mean the default. */
export function readSessionRowQuickActions(): boolean {
  return preference.read() === "true";
}

/** Persist the preference and notify sidebars already on screen. */
export function writeSessionRowQuickActions(shown: boolean): void {
  preference.write(shown ? "true" : "false");
}

/** Re-read after a same-tab write that bypassed `writeSessionRowQuickActions` (settings import). */
export const notifySessionRowQuickActionsChanged = preference.notifyChanged;

/** Live value of the preference, including changes made in other windows. */
export function useSessionRowQuickActions(): boolean {
  return preference.use() === "true";
}
