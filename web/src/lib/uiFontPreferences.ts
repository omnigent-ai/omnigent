// Persisted, app-global preference for the UI font size.
//
// The web UI is Tailwind v4, which sizes typography AND spacing in `rem`, so
// scaling the root `<html>` font-size reflows the entire UI uniformly. Rather
// than write an inline `html { font-size }` — which would override the mobile
// `@media` bump in index.css — this stores an absolute px choice and applies it
// as a scale multiplier (`--ui-font-scale`) that the root font-size rules
// multiply into. The base rule uses `calc(1em * var(--ui-font-scale))`, so the
// user's browser-default size is preserved and the displayed px maps 1:1 for
// the default-16px case.

const STORAGE_KEY = "omnigent:ui-font-size";

/** Reference size that a scale of 1 corresponds to (Tailwind/browser default). */
const BASE_FONT_SIZE_PX = 16;

export const UI_FONT_SIZE_DEFAULT = 16;
export const UI_FONT_SIZE_MIN = 12;
export const UI_FONT_SIZE_MAX = 20;
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
 * Apply the given px size to the DOM by setting the `--ui-font-scale` variable
 * on the document root. The root font-size rules in index.css multiply this in,
 * so the whole rem-based UI (text + spacing) scales, and the mobile bump still
 * composes on top. This is the single source of the DOM side-effect.
 */
export function applyUiFontScale(px: number): void {
  if (typeof document === "undefined") return;
  const scale = clampUiFontSizePx(px) / BASE_FONT_SIZE_PX;
  document.documentElement.style.setProperty("--ui-font-scale", String(scale));
}
