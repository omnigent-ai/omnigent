export const themeModes = ["light", "dark", "system"] as const;

export type ThemeMode = (typeof themeModes)[number];
export type ResolvedThemeMode = Exclude<ThemeMode, "system">;

/**
 * Return whether a string is one of the selectable theme modes.
 *
 * `next-themes` reads from localStorage, so the input can be any stale
 * or user-edited string. Keeping this as a type guard lets call sites
 * reject unsupported values before handing them to UI controls.
 *
 * @param value Theme string to validate, e.g. `"dark"`.
 * @returns Whether the value is a supported theme mode.
 */
export function isThemeMode(value: string | undefined): value is ThemeMode {
  return value === "light" || value === "dark" || value === "system";
}

/**
 * Normalize persisted theme selection to the app's default system mode.
 *
 * Unknown values can only come from localStorage drift or manual edits.
 * Falling back to `system` matches the provider's documented default
 * and avoids rendering a menu with no selected radio item.
 *
 * @param value Stored theme string, e.g. `"system"`.
 * @returns Supported theme mode to use in controls.
 */
export function normalizeThemeMode(value: string | undefined): ThemeMode {
  return isThemeMode(value) ? value : "system";
}

/**
 * Normalize the currently rendered palette to a concrete light/dark mode.
 *
 * `resolvedTheme` is normally `"light"` or `"dark"` once next-themes has
 * evaluated system preference. During initial render it can be undefined,
 * so light is the conservative non-system fallback for icon and Shiki
 * theme selection until the provider updates.
 *
 * @param value Resolved theme string from next-themes, e.g. `"dark"`.
 * @returns Concrete light or dark rendering mode.
 */
export function normalizeResolvedTheme(value: string | undefined): ResolvedThemeMode {
  return value === "dark" ? "dark" : "light";
}

/**
 * Return the next mode in the system → dark → light click cycle.
 *
 * The theme control cycles on click rather than opening a menu. The
 * cycle starts at system (the pre-selection default), so the first
 * click pins dark, the next pins light, and the next returns to
 * following the OS preference.
 *
 * When the resolved appearance is provided, redundant transitions are
 * skipped — e.g. "system" already rendering as dark jumps straight to
 * light instead of offering "Switch to Dark".
 *
 * @param mode Current selectable theme mode, e.g. `"dark"`.
 * @param resolvedTheme The actual rendered palette, e.g. `"dark"`.
 * @returns The mode to apply on the next click, e.g. `"light"`.
 */
export function nextThemeMode(mode: ThemeMode, resolvedTheme?: string): ThemeMode {
  const cycle: Record<ThemeMode, ThemeMode> = {
    system: "dark",
    dark: "light",
    light: "system",
  };
  const next = cycle[mode];
  if (resolvedTheme && next !== "system" && next === resolvedTheme) {
    return cycle[next];
  }
  return next;
}
