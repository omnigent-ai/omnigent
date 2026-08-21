// Persisted, app-global preferences for the UI font — size and family.
//
// The preference is stored as a discrete px choice and exposed to CSS through
// `--desktop-ui-font-size`. index.css maps that value into Tailwind's typography
// tokens at desktop widths while keeping the root rem grid fixed at 16px, so
// text changes without resizing icons, controls, or spacing. Mobile keeps its
// independent responsive root size and typography.
//
// Font family works the analogous way with `--ui-font-family`. Note it can't
// reuse `--font-sans`: Tailwind v4's `@theme inline` block inlines the literal
// stack into the `font-sans` utility instead of a `var()` reference, so setting
// `--font-sans` at runtime is a no-op. The `html` rule reads
// `var(--ui-font-family, var(--font-sans))`, so an unset family falls back to
// the system stack and any value we set on documentElement wins.

import { createCssFontFamilyPreference } from "./cssFontFamilyPreference";

const STORAGE_KEY = "omnigent:ui-font-size";

export const UI_FONT_SIZE_DEFAULT = 13;
export const UI_FONT_SIZE_MIN = 11;
export const UI_FONT_SIZE_MAX = 18;
export const UI_FONT_SIZE_STEP = 1;

/** Clamp an arbitrary number into the supported px range. */
export function clampUiFontSizePx(px: number): number {
  return Math.min(UI_FONT_SIZE_MAX, Math.max(UI_FONT_SIZE_MIN, Math.round(px)));
}

function isValidPx(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Read the persisted UI font size in px.
 *
 * Returns the default when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/malformed — never throws, so a corrupt
 * entry can't break app boot. A stored value outside the range is clamped.
 */
export function readUiFontSizePx(): number {
  if (typeof window === "undefined") return UI_FONT_SIZE_DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return UI_FONT_SIZE_DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    if (!isValidPx(parsed)) return UI_FONT_SIZE_DEFAULT;
    return clampUiFontSizePx(parsed);
  } catch {
    return UI_FONT_SIZE_DEFAULT;
  }
}

/**
 * Persist the UI font size (px). The value is clamped to the supported range
 * before writing. Swallows quota/access errors so a failed write can't break
 * the app.
 */
export function writeUiFontSizePx(px: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(clampUiFontSizePx(px)));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/**
 * Apply the given discrete px size to the DOM by setting the
 * `--desktop-ui-font-size` variable on the document root. index.css reads this
 * into desktop typography tokens only, so layout geometry and mobile remain
 * independent. This is the single source of the DOM side-effect.
 */
export function applyDesktopUiFontSize(px: number): void {
  if (typeof document === "undefined") return;
  document.documentElement.style.setProperty(
    "--desktop-ui-font-size",
    `${clampUiFontSizePx(px)}px`,
  );
}

// ---- Font family ---------------------------------------------------------

const FONT_FAMILY_STORAGE_KEY = "omnigent:ui-font-family";

/** Empty string = "System default": no override, falls back to `--font-sans`. */
export const UI_FONT_FAMILY_DEFAULT = "";

/**
 * The sans stack the UI font falls back to when no custom family is set (or an
 * uninstalled name is chosen). It's the `--font-sans` variable rather than a
 * literal so the CSS var and the appended fallback stay in lockstep, and so
 * SettingsPage can name the fallback semantically instead of repeating the
 * literal (mirrors {@link CODE_FONT_FAMILY_FALLBACK}).
 */
export const UI_FONT_FAMILY_FALLBACK = "var(--font-sans)";

// The whole UI font-family preference — read/normalize/persist/apply — is the
// shared CSS-variable-backed shape. Setting `--ui-font-family` on the document
// root drives the `html` rule in index.css; the `sans` category loads the right
// catalog entry for a shared family.
const uiFontFamilyPreference = createCssFontFamilyPreference({
  key: FONT_FAMILY_STORAGE_KEY,
  cssVar: "--ui-font-family",
  fallback: UI_FONT_FAMILY_FALLBACK,
  category: "sans",
});

/**
 * Read the persisted UI font family.
 *
 * Returns "" (System default) when nothing is stored, on a server render (no
 * `window`), or when the stored value is missing/malformed — never throws, so a
 * corrupt entry can't break app boot.
 */
export function readUiFontFamily(): string {
  return uiFontFamilyPreference.read();
}

/**
 * Persist the UI font family. An empty (or all-stripped) name clears the
 * preference — reverting to System default — rather than storing a blank. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeUiFontFamily(name: string): void {
  uiFontFamilyPreference.write(name);
}

/**
 * Apply the given family to the DOM by setting the `--ui-font-family` variable
 * on the document root; the `html` rule in index.css reads it as the whole UI's
 * font. An empty name removes the property, restoring the system stack.
 *
 * The chosen family is applied WITH the system stack appended
 * (`<name>, var(--font-sans)`) so a name that isn't installed — or a partial one
 * typed so far — degrades to the app's default sans rather than the browser's
 * default serif. Also kicks a fire-and-forget webfont load for a catalog family.
 * This is the single source of the DOM side-effect.
 */
export function applyUiFontFamily(name: string): void {
  uiFontFamilyPreference.apply(name);
}
