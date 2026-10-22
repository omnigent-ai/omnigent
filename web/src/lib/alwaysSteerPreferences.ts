// Persisted, per-device preference for how a message sent while the agent is
// busy is dispatched.
//
// Normally a follow-up typed mid-turn is parked in the client-side queue strip
// (editable / reorderable) and auto-flushed when the agent goes idle. When this
// opt-in preference is on, a follow-up skips the queue and is POSTed
// immediately — steered into the running turn where the harness supports live
// injection, folded in at the next turn boundary otherwise. It's a device-local
// dispatch preference — no account or session state changes — so it lives in
// localStorage like the other `*Preferences` helpers.

const STORAGE_KEY = "omnigent:always-steer";

export const DEFAULT_ALWAYS_STEER = false;

/**
 * Read the persisted "always steer" preference. Returns the default (off) when
 * nothing is stored, on a server render (no `window`), or when the stored value
 * is malformed — never throws, so a corrupt entry can't break the app.
 */
export function readAlwaysSteer(): boolean {
  if (typeof window === "undefined") return DEFAULT_ALWAYS_STEER;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_ALWAYS_STEER;
    return raw === "true";
  } catch {
    return DEFAULT_ALWAYS_STEER;
  }
}

/**
 * Persist the "always steer" preference. Swallows quota/access errors so a
 * failed write can't break the app.
 */
export function writeAlwaysSteer(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
