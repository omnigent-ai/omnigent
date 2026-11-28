// Per-device choice to show Otto's small playful touches: the tide-pool welcome on a new
// project's board, a "Done" mark on finished tickets you asked for, friendlier empty
// lanes, Otto at Inbox zero and milestone notes. Off, every one of them falls back to the
// plain version.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const PLAYFUL_TOUCHES_STORAGE_KEY = "omnigent:playful-touches";

export const DEFAULT_PLAYFUL_TOUCHES = true;

const preference = createStoredChoicePreference(
  PLAYFUL_TOUCHES_STORAGE_KEY,
  ["false", "true"],
  DEFAULT_PLAYFUL_TOUCHES ? "true" : "false",
);

/** Read the preference; storage errors and unknown values mean the default. */
export function readPlayfulTouches(): boolean {
  return preference.read() === "true";
}

/** Persist the preference and update every surface on screen. */
export function writePlayfulTouches(enabled: boolean): void {
  preference.write(enabled ? "true" : "false");
}

/** Re-read after a same-tab write that bypassed `writePlayfulTouches` (settings import). */
export const notifyPlayfulTouchesChanged = preference.notifyChanged;

/** Live value of the preference, including changes made in other windows. */
export function usePlayfulTouches(): boolean {
  return preference.use() === "true";
}
