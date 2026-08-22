export const DEFAULT_BOTTOM_LOCK_ENABLED = true;
export const BOTTOM_LOCK_STORAGE_KEY = "omnigent:bottom-lock";

const CHANGE_EVENT = "omnigent:bottom-lock-change";

export function readBottomLockEnabled(): boolean {
  if (typeof window === "undefined") return DEFAULT_BOTTOM_LOCK_ENABLED;
  try {
    const stored = window.localStorage.getItem(BOTTOM_LOCK_STORAGE_KEY);
    return stored === null ? DEFAULT_BOTTOM_LOCK_ENABLED : stored === "true";
  } catch {
    return DEFAULT_BOTTOM_LOCK_ENABLED;
  }
}

export function writeBottomLockEnabled(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (enabled === DEFAULT_BOTTOM_LOCK_ENABLED) {
      window.localStorage.removeItem(BOTTOM_LOCK_STORAGE_KEY);
    } else {
      window.localStorage.setItem(BOTTOM_LOCK_STORAGE_KEY, String(enabled));
    }
  } catch {
    // localStorage access errors are non-fatal.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function subscribeBottomLockEnabled(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}
