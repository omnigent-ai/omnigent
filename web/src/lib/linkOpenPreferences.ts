// Per-device preference: open plain-clicked chat links in the desktop shell's
// in-app browser instead of the default external browser. Off by default.

const STORAGE_KEY = "omnigent:open-links-in-app";

export function readOpenLinksInApp(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeOpenLinksInApp(value: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(value));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
