// Whether a page shows its sidebar (a file tree), remembered per device under the page's key.

export function readSidebarVisible(storageKey: string, fallback: boolean): boolean {
  try {
    const stored = window.localStorage.getItem(storageKey);
    if (stored === "true") return true;
    if (stored === "false") return false;
  } catch {
    // Unreadable storage falls back to the default.
  }
  return fallback;
}

export function writeSidebarVisible(storageKey: string, visible: boolean): void {
  try {
    window.localStorage.setItem(storageKey, String(visible));
  } catch {
    // A storage failure only loses the preference.
  }
}
