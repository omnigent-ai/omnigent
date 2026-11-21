// Persisted, per-device preference for how a message sent while the agent is
// busy is dispatched.
//
// Normally a follow-up typed mid-turn is parked in the client-side queue strip
// (editable / reorderable) and auto-flushed when the agent goes idle. When this
// preference is on (the default), a follow-up skips the queue and is POSTed
// immediately — steered into the running turn where the harness supports live
// injection, folded in at the next turn boundary otherwise. It's a device-local
// dispatch preference — no account or session state changes — so it lives in
// localStorage like the other `*Preferences` helpers.

import { createStoredChoicePreference } from "./storedChoicePreference";

const STORAGE_KEY = "omnigent:always-steer";

export const DEFAULT_ALWAYS_STEER = true;

const preference = createStoredChoicePreference(
  STORAGE_KEY,
  ["false", "true"],
  DEFAULT_ALWAYS_STEER ? "true" : "false",
);

/**
 * Read the persisted "always steer" preference. Returns the default (on) when
 * nothing is stored, on a server render (no `window`), or when the stored value
 * is malformed — never throws, so a corrupt entry can't break the app.
 */
export function readAlwaysSteer(): boolean {
  return preference.read() === "true";
}

/**
 * Persist the "always steer" preference and notify readers on screen. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeAlwaysSteer(value: boolean): void {
  preference.write(value ? "true" : "false");
}

/** Live value of the preference, including changes from ⌘K or another window. */
export function useAlwaysSteer(): boolean {
  return preference.use() === "true";
}
