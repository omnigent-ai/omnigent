// Persisted preference for the terminal's light/dark palette — independent of
// the app chrome theme. The terminal is an xterm.js `ITheme` JS object applied
// imperatively (unlike the chrome theme, which rides the `.dark` class + CSS
// vars), so a mid-session change is pushed to mounted terminals via a pub/sub;
// `auto` follows the app's resolved theme while `light`/`dark` pin it.

const STORAGE_KEY = "omnigent:terminal-theme";

export const terminalThemeModes = ["auto", "light", "dark"] as const;
export type TerminalThemeMode = (typeof terminalThemeModes)[number];
export const TERMINAL_THEME_DEFAULT: TerminalThemeMode = "auto";

/** Return whether a string is one of the selectable terminal theme modes. */
export function isTerminalThemeMode(value: string | null | undefined): value is TerminalThemeMode {
  return value === "auto" || value === "light" || value === "dark";
}

/**
 * Normalize a stored terminal theme string to the default auto mode.
 *
 * Unknown values can only come from localStorage drift or manual edits.
 * Falling back to `auto` matches the documented default and preserves
 * backwards-compatible "follow the app" behavior.
 */
export function normalizeTerminalThemeMode(value: string | null | undefined): TerminalThemeMode {
  return isTerminalThemeMode(value) ? value : TERMINAL_THEME_DEFAULT;
}

/**
 * Read the persisted terminal theme mode.
 *
 * Returns "auto" when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/unknown — never throws, so a corrupt
 * entry can't break app boot.
 */
export function readTerminalThemeMode(): TerminalThemeMode {
  if (typeof window === "undefined") return TERMINAL_THEME_DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return TERMINAL_THEME_DEFAULT;
    return normalizeTerminalThemeMode(raw);
  } catch {
    return TERMINAL_THEME_DEFAULT;
  }
}

/**
 * Persist the terminal theme mode, then notify subscribers so mounted
 * terminals re-apply it live. "auto" clears the key (the default). Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeTerminalThemeMode(mode: TerminalThemeMode): void {
  const normalized = normalizeTerminalThemeMode(mode);
  if (typeof window !== "undefined") {
    try {
      if (normalized === TERMINAL_THEME_DEFAULT) {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, normalized);
      }
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  }
  // Broadcast the intended value, not a storage re-read: if the write above
  // failed (quota/denied), mounted terminals must still re-theme now rather
  // than snapping back to the stale/default stored value.
  emit(normalized);
}

/**
 * Resolve whether the terminal should render dark given the user's mode and
 * the app's current resolved appearance.
 */
export function resolveTerminalIsDark(mode: TerminalThemeMode, appIsDark: boolean): boolean {
  switch (mode) {
    case "auto":
      return appIsDark;
    case "light":
      return false;
    case "dark":
      return true;
    default: {
      const _exhaustive: never = mode;
      return _exhaustive;
    }
  }
}

type TerminalThemeListener = (mode: TerminalThemeMode) => void;

const listeners = new Set<TerminalThemeListener>();

/**
 * Subscribe to terminal theme changes. The callback fires with the current
 * {@link TerminalThemeMode} whenever it is written (e.g. from Settings),
 * letting an already-mounted terminal re-apply the palette live — xterm's
 * `ITheme` can't ride a CSS variable the way the chrome theme does. Returns
 * an unsubscribe function.
 */
export function subscribeTerminalTheme(listener: TerminalThemeListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Notify subscribers of the given terminal theme mode. Called after every write. */
function emit(mode: TerminalThemeMode): void {
  for (const listener of listeners) listener(mode);
}
