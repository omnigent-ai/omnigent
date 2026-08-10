const STORAGE_KEY = "omnigent:archived-retention-days";

export function readRetentionDays(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === null) return null;
    const parsed = parseInt(stored, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  } catch {
    return null;
  }
}

export function writeRetentionDays(days: number | null): void {
  if (typeof window === "undefined") return;
  try {
    if (days === null) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, String(days));
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
