// Persisted preference for the terminal's light/dark palette — independent of
// the app chrome theme. The terminal is an xterm.js `ITheme` JS object applied
// imperatively (unlike the chrome theme, which rides the `.dark` class + CSS
// vars), so a mid-session change is pushed to mounted terminals via a pub/sub;
// `auto` follows the app's resolved theme while `light`/`dark` pin it.
//
// Owned by {@link createLocalPreference}; storage key and raw-string format
// are unchanged so existing localStorage values keep working.

import { createLocalPreference } from "@/lib/preferences";

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
 * Declarative terminal theme preference. Same key and raw `"auto"`/`"light"`/
 * `"dark"` string format as before — no migration rewrite of stored values.
 */
export const terminalThemePreference = createLocalPreference<TerminalThemeMode>({
  key: "omnigent:terminal-theme",
  defaultValue: TERMINAL_THEME_DEFAULT,
  parse: (raw) => normalizeTerminalThemeMode(raw),
  serialize: (value) => value,
  normalize: normalizeTerminalThemeMode,
  clearWhenDefault: true,
  appearance: true,
});

/** Read the persisted terminal theme mode. */
export function readTerminalThemeMode(): TerminalThemeMode {
  return terminalThemePreference.read();
}

/** Persist the terminal theme mode and notify subscribers. */
export function writeTerminalThemeMode(mode: TerminalThemeMode): void {
  terminalThemePreference.write(mode);
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
      const exhaustive: never = mode;
      return exhaustive;
    }
  }
}

/**
 * Subscribe to terminal theme changes. The callback fires with the current
 * {@link TerminalThemeMode} whenever it is written (e.g. from Settings),
 * letting an already-mounted terminal re-apply the palette live — xterm's
 * `ITheme` can't ride a CSS variable the way the chrome theme does. Returns
 * an unsubscribe function.
 */
export function subscribeTerminalTheme(listener: (mode: TerminalThemeMode) => void): () => void {
  return terminalThemePreference.subscribeValue(listener);
}
