// Shared core for CSS-variable-backed font-family preferences.
//
// The chrome/UI font (uiFontPreferences.ts) and the general monospace font both
// ride a CSS custom property on the document root: an unset property falls back
// to a system stack, and a set value wins. The read/normalize/persist/apply
// mechanics are identical across them — only the storage key, the CSS variable,
// the appended fallback stack, and the loader category differ. This module
// factors that common core out so each family is a thin config object.
//
// The code font (codeFontPreferences.ts) is deliberately NOT built on this: it
// can't ride a CSS variable (Monaco/xterm are fixed-pixel widgets) and needs a
// specialized immediate + post-load remeasure/refit pub/sub path.
//
// SSR/no-DOM safe: reads return the empty default with no `window`, and apply
// no-ops with no `document`, so boot-time restore on the server is harmless.

import { loadFontByFamily } from "./webFontLoader";
import type { FontCategory } from "./fontCatalog";

/** Empty string = the family's default: no override, falls back to the stack. */
export const FONT_FAMILY_DEFAULT = "";

/** Longest family name we'll accept — a guard against a corrupt/oversized entry. */
const FONT_FAMILY_MAX_LENGTH = 100;

/**
 * Normalize a raw family name into a value safe to persist and to set as a CSS
 * custom property (or hand a code widget): trimmed, with characters that could
 * terminate the declaration or open a new one (`;{}` and control chars)
 * stripped. Over-long input collapses to the default. Returns "" for anything
 * that isn't a usable family, so callers treat empty as the family default.
 */
export function normalizeFontFamily(value: unknown): string {
  if (typeof value !== "string") return FONT_FAMILY_DEFAULT;
  // eslint-disable-next-line no-control-regex -- intentionally stripping control chars
  const cleaned = value.replace(/[;{}\x00-\x1f\x7f]/g, "").trim();
  if (!cleaned || cleaned.length > FONT_FAMILY_MAX_LENGTH) {
    return FONT_FAMILY_DEFAULT;
  }
  return cleaned;
}

/**
 * Read a persisted font family from `localStorage[key]`.
 *
 * Returns "" (the family default) when nothing is stored, on a server render (no
 * `window`), or when the stored value is missing/malformed — never throws, so a
 * corrupt entry can't break app boot.
 */
export function readStoredFontFamily(key: string): string {
  if (typeof window === "undefined") return FONT_FAMILY_DEFAULT;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return FONT_FAMILY_DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    return normalizeFontFamily(parsed);
  } catch {
    return FONT_FAMILY_DEFAULT;
  }
}

/**
 * Persist a font family under `localStorage[key]`, returning the normalized name
 * that was applied. An empty (or all-stripped) name clears the preference —
 * reverting to the family default — rather than storing a blank. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeStoredFontFamily(key: string, name: string): string {
  const normalized = normalizeFontFamily(name);
  if (typeof window === "undefined") return normalized;
  try {
    if (!normalized) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, JSON.stringify(normalized));
    }
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
  return normalized;
}

/** Config for a CSS-variable-backed font-family preference. */
export interface CssFontFamilyPreferenceConfig {
  /** localStorage key the value is persisted under. */
  readonly key: string;
  /** CSS custom property set on the document root, e.g. `--ui-font-family`. */
  readonly cssVar: string;
  /**
   * Fallback stack appended after the chosen family in the CSS value, e.g.
   * `var(--font-sans)`. So an uninstalled/partial name degrades to this rather
   * than the browser's default serif. The `var(--x, …)` fallback in the CSS only
   * fires when the property is UNSET, not when it holds an unusable name, so the
   * fallback has to live inside the value too.
   */
  readonly fallback: string;
  /** Loader category, so a family shared across roles loads the right entry. */
  readonly category: FontCategory;
}

/** The read/write/apply trio for one CSS-variable-backed font family. */
export interface CssFontFamilyPreference {
  /** Read the persisted family ("" = default). Never throws. */
  read(): string;
  /** Persist the family; returns the normalized name written ("" cleared it). */
  write(name: string): string;
  /**
   * Apply the family to the document root's CSS variable. An empty name removes
   * the property (restoring the fallback stack); a catalog name also kicks a
   * fire-and-forget webfont load so the glyphs arrive (font-display: swap paints
   * the swap). This is the single source of the DOM side-effect.
   */
  apply(name: string): void;
}

/**
 * Build the read/write/apply trio for a CSS-variable-backed font family (the
 * UI and fixed-width shape). See {@link CssFontFamilyPreferenceConfig}.
 */
export function createCssFontFamilyPreference(
  config: CssFontFamilyPreferenceConfig,
): CssFontFamilyPreference {
  const { key, cssVar, fallback, category } = config;
  return {
    read: () => readStoredFontFamily(key),
    write: (name: string) => writeStoredFontFamily(key, name),
    apply: (name: string) => {
      if (typeof document === "undefined") return;
      const normalized = normalizeFontFamily(name);
      if (!normalized) {
        document.documentElement.style.removeProperty(cssVar);
        return;
      }
      // Kick a webfont load when the name matches a catalog family so the glyphs
      // actually arrive (fire-and-forget: the CSS var is set now and
      // font-display: swap paints the face once it lands). A non-catalog name is
      // left to the OS — the existing free-text behavior. The `, <fallback>`
      // covers the gap before load and any name that never resolves.
      void loadFontByFamily(normalized, category);
      document.documentElement.style.setProperty(cssVar, `${normalized}, ${fallback}`);
    },
  };
}
