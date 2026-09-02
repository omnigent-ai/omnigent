// Persisted preference for the terminal extra-keys row (Esc / Tab / Ctrl /
// arrows under the terminal). `auto` shows it on touch-capable devices, `on`
// forces it (desktop, convertibles, Electron), `off` hides it (a tablet with
// a full hardware keyboard). Mirrors terminalThemePreferences: localStorage
// key, typed read/write, and a pub/sub for useSyncExternalStore.

const STORAGE_KEY = "omnigent:terminal-extra-keys";

export const terminalExtraKeysModes = ["auto", "on", "off"] as const;
export type TerminalExtraKeysMode = (typeof terminalExtraKeysModes)[number];
export const TERMINAL_EXTRA_KEYS_DEFAULT: TerminalExtraKeysMode = "auto";

/** Return whether a string is one of the selectable extra-keys modes. */
export function isTerminalExtraKeysMode(
  value: string | null | undefined,
): value is TerminalExtraKeysMode {
  return value === "auto" || value === "on" || value === "off";
}

/** Normalize a stored value; unknown or corrupt entries fall back to `auto`. */
export function normalizeTerminalExtraKeysMode(
  value: string | null | undefined,
): TerminalExtraKeysMode {
  return isTerminalExtraKeysMode(value) ? value : TERMINAL_EXTRA_KEYS_DEFAULT;
}

/**
 * Read the persisted extra-keys mode. Returns `auto` when nothing is stored,
 * on a server render, or when storage is inaccessible — never throws.
 */
export function readTerminalExtraKeysMode(): TerminalExtraKeysMode {
  if (typeof window === "undefined") return TERMINAL_EXTRA_KEYS_DEFAULT;
  try {
    return normalizeTerminalExtraKeysMode(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return TERMINAL_EXTRA_KEYS_DEFAULT;
  }
}

/**
 * Persist the extra-keys mode, then notify subscribers so mounted terminals
 * show or hide the row live. `auto` clears the key. Swallows quota/access
 * errors so a failed write can't break the app.
 */
export function writeTerminalExtraKeysMode(mode: TerminalExtraKeysMode): void {
  const normalized = normalizeTerminalExtraKeysMode(mode);
  if (typeof window !== "undefined") {
    try {
      if (normalized === TERMINAL_EXTRA_KEYS_DEFAULT) {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, normalized);
      }
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  }
  emit(normalized);
}

type TerminalExtraKeysListener = (mode: TerminalExtraKeysMode) => void;

const listeners = new Set<TerminalExtraKeysListener>();

/**
 * Subscribe to extra-keys mode changes; fires with the new mode after every
 * write. Returns an unsubscribe function. The signature doubles as the
 * `subscribe` argument of `useSyncExternalStore`.
 */
export function subscribeTerminalExtraKeys(listener: TerminalExtraKeysListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function emit(mode: TerminalExtraKeysMode): void {
  for (const listener of listeners) listener(mode);
}
