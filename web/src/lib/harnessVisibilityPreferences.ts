// Persisted, per-device preference for which harnesses the new-chat picker
// shows.
//
// The picker normally lists every harness and badges the ones that aren't set
// up on the selected host ("needs setup" / "binary missing" / "needs auth").
// When this opt-in preference is on, those unconfigured harnesses are hidden
// instead of badged, so the picker only offers harnesses that can actually
// launch on the chosen host. It's a device-local UI filter — no account or
// host state is changed — so it lives in localStorage like the other
// `*Preferences` helpers.

const STORAGE_KEY = "omnigent:hide-unconfigured-harnesses";

export const DEFAULT_HIDE_UNCONFIGURED_HARNESSES = false;

/**
 * Read the persisted "hide unconfigured harnesses" preference. Returns the
 * default (off) when nothing is stored, on a server render (no `window`), or
 * when the stored value is malformed — never throws, so a corrupt entry can't
 * break the app.
 */
export function readHideUnconfiguredHarnesses(): boolean {
  if (typeof window === "undefined") return DEFAULT_HIDE_UNCONFIGURED_HARNESSES;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_HIDE_UNCONFIGURED_HARNESSES;
    return raw === "true";
  } catch {
    return DEFAULT_HIDE_UNCONFIGURED_HARNESSES;
  }
}

/**
 * Persist the "hide unconfigured harnesses" preference. Swallows quota/access
 * errors so a failed write can't break the app.
 */
export function writeHideUnconfiguredHarnesses(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
