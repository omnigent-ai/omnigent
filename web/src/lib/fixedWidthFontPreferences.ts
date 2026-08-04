// Persisted, app-global preference for the FIXED-WIDTH font — the family used
// by general monospace UI chrome (the `font-mono` utility: file paths, hashes,
// inline code chips, log rows, …). This is a distinct role from both the
// chrome/UI sans font (see lib/uiFontPreferences.ts) and the code editor /
// terminal font (see lib/codeFontPreferences.ts).
//
// Like the UI sans font it rides a CSS custom property, `--ui-mono-font-family`,
// so it uses the shared CSS-variable-backed shape. It can't reuse `--font-mono`:
// Tailwind v4's `@theme inline` block inlines the literal mono stack into the
// `font-mono` utility rather than a `var()`, so setting `--font-mono` at runtime
// is a no-op. An unlayered `.font-mono` rule in index.css reads
// `var(--ui-mono-font-family, var(--font-mono))`, so an unset preference falls
// back to the app mono stack and any value we set on documentElement wins.

import { createCssFontFamilyPreference } from "./cssFontFamilyPreference";

const FONT_FAMILY_STORAGE_KEY = "omnigent:fixed-width-font-family";

/** Empty string = "Default": no override, falls back to the `--font-mono` stack. */
export const FIXED_WIDTH_FONT_FAMILY_DEFAULT = "";

/**
 * The mono stack the fixed-width font falls back to when no custom family is set
 * (or an uninstalled name is chosen). It's the `--font-mono` variable rather
 * than a literal so the CSS var and the appended fallback stay in lockstep
 * (mirrors {@link UI_FONT_FAMILY_FALLBACK}).
 */
export const FIXED_WIDTH_FONT_FAMILY_FALLBACK = "var(--font-mono)";

// The whole fixed-width font-family preference — read/normalize/persist/apply —
// is the shared CSS-variable-backed shape. Setting `--ui-mono-font-family` on
// the document root drives the `.font-mono` rule in index.css; the `fixedWidth`
// category loads the right catalog entry for a shared family.
const fixedWidthFontFamilyPreference = createCssFontFamilyPreference({
  key: FONT_FAMILY_STORAGE_KEY,
  cssVar: "--ui-mono-font-family",
  fallback: FIXED_WIDTH_FONT_FAMILY_FALLBACK,
  category: "fixedWidth",
});

/**
 * Read the persisted fixed-width font family.
 *
 * Returns "" (Default) when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/malformed — never throws, so a corrupt
 * entry can't break app boot.
 */
export function readFixedWidthFontFamily(): string {
  return fixedWidthFontFamilyPreference.read();
}

/**
 * Persist the fixed-width font family. An empty (or all-stripped) name clears
 * the preference — reverting to Default — rather than storing a blank. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeFixedWidthFontFamily(name: string): void {
  fixedWidthFontFamilyPreference.write(name);
}

/**
 * Apply the given family to the DOM by setting the `--ui-mono-font-family`
 * variable on the document root; the `.font-mono` rule in index.css reads it as
 * the monospace-chrome font. An empty name removes the property, restoring the
 * `--font-mono` stack.
 *
 * The chosen family is applied WITH the mono stack appended
 * (`<name>, var(--font-mono)`) so a name that isn't installed — or a partial one
 * typed so far — degrades to the app mono rather than a browser default. Also
 * kicks a fire-and-forget webfont load for a catalog family. This is the single
 * source of the DOM side-effect.
 */
export function applyFixedWidthFontFamily(name: string): void {
  fixedWidthFontFamilyPreference.apply(name);
}
