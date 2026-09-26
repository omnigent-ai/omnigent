// Persisted, per-device preference for where a plain click on a web link in
// chat content opens on the desktop shell.
//
// Off (the default), a normal click keeps the current behavior: the shell's
// window-open policy hands the link to the default external browser. When
// this opt-in preference is on, a normal click routes the link into the
// conversation's embedded in-app browser pane instead; modified clicks
// (cmd/ctrl/shift/alt) always keep the external-browser behavior. It's a
// device-local UI preference — no account or session state changes — so it
// lives in localStorage like the other `*Preferences` helpers.

const STORAGE_KEY = "omnigent:open-links-in-app";

export const DEFAULT_OPEN_LINKS_IN_APP = false;

/**
 * Read the persisted "open links in the in-app browser" preference. Returns
 * the default (off) when nothing is stored, on a server render (no `window`),
 * or when the stored value is malformed — never throws, so a corrupt entry
 * can't break the app.
 */
export function readOpenLinksInApp(): boolean {
  if (typeof window === "undefined") return DEFAULT_OPEN_LINKS_IN_APP;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_OPEN_LINKS_IN_APP;
    return raw === "true";
  } catch {
    return DEFAULT_OPEN_LINKS_IN_APP;
  }
}

/**
 * Persist the "open links in the in-app browser" preference. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeOpenLinksInApp(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
