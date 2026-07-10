// Persisted, app-global preferences for the UI font — size and family.
//
// The web UI is Tailwind v4, which sizes typography AND spacing in `rem`, so
// scaling the root `<html>` font-size reflows the entire UI uniformly. Rather
// than write an inline `html { font-size }` — which would override the mobile
// `@media` bump in index.css — this stores an absolute px choice and applies it
// as a scale multiplier (`--ui-font-scale`) that the root font-size rules
// multiply into. The base rule uses `calc(1em * var(--ui-font-scale))`, so the
// user's browser-default size is preserved and the displayed px maps 1:1 for
// the default-16px case.
//
// Font family works the analogous way with `--ui-font-family`. Note it can't
// reuse `--font-sans`: Tailwind v4's `@theme inline` block inlines the literal
// stack into the `font-sans` utility instead of a `var()` reference, so setting
// `--font-sans` at runtime is a no-op. The `html` rule reads
// `var(--ui-font-family, var(--font-sans))`, so an unset family falls back to
// the system stack and any value we set on documentElement wins.

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

// ---- Font family ---------------------------------------------------------

const FONT_FAMILY_STORAGE_KEY = "omnigent:ui-font-family";

/** Empty string = "System default": no override, falls back to `--font-sans`. */
export const UI_FONT_FAMILY_DEFAULT = "";

/** Longest family name we'll accept — a guard against a corrupt/oversized entry. */
const UI_FONT_FAMILY_MAX_LENGTH = 100;

/**
 * Normalize a raw family name into a value safe to persist and to set as a CSS
 * custom property: trimmed, with characters that could terminate the
 * declaration or open a new one (`;{}` and control chars) stripped. Over-long
 * input collapses to the default. Returns "" for anything that isn't a usable
 * family, so callers treat empty as "System default".
 */
function normalizeUiFontFamily(value: unknown): string {
  if (typeof value !== "string") return UI_FONT_FAMILY_DEFAULT;
  // eslint-disable-next-line no-control-regex -- intentionally stripping control chars
  const cleaned = value.replace(/[;{}\x00-\x1f\x7f]/g, "").trim();
  if (!cleaned || cleaned.length > UI_FONT_FAMILY_MAX_LENGTH) {
    return UI_FONT_FAMILY_DEFAULT;
  }
  return cleaned;
}

/**
 * Read the persisted UI font family.
 *
 * Returns "" (System default) when nothing is stored, on a server render (no
 * `window`), or when the stored value is missing/malformed — never throws, so a
 * corrupt entry can't break app boot.
 */
export function readUiFontFamily(): string {
  if (typeof window === "undefined") return UI_FONT_FAMILY_DEFAULT;
  try {
    const raw = window.localStorage.getItem(FONT_FAMILY_STORAGE_KEY);
    if (!raw) return UI_FONT_FAMILY_DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    return normalizeUiFontFamily(parsed);
  } catch {
    return UI_FONT_FAMILY_DEFAULT;
  }
}

/**
 * Persist the UI font family. An empty (or all-stripped) name clears the
 * preference — reverting to System default — rather than storing a blank. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeUiFontFamily(name: string): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeUiFontFamily(name);
    if (!normalized) {
      window.localStorage.removeItem(FONT_FAMILY_STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(FONT_FAMILY_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/**
 * Apply the given family to the DOM by setting the `--ui-font-family` variable
 * on the document root; the `html` rule in index.css reads it as the whole UI's
 * font. An empty name removes the property, restoring the system stack.
 *
 * The chosen family is applied WITH the system stack appended
 * (`<name>, var(--font-sans)`) so a name that isn't installed — or a partial one
 * typed so far — degrades to the app's default sans rather than the browser's
 * default serif. (The `var(--ui-font-family, …)` fallback in the CSS only fires
 * when the property is unset, not when it holds an unusable name, so the
 * fallback has to live inside the value too.) This is the single source of the
 * DOM side-effect.
 */
export function applyUiFontFamily(name: string): void {
  if (typeof document === "undefined") return;
  const normalized = normalizeUiFontFamily(name);
  if (!normalized) {
    document.documentElement.style.removeProperty("--ui-font-family");
    return;
  }
  document.documentElement.style.setProperty("--ui-font-family", `${normalized}, var(--font-sans)`);
}
