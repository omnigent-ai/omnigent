// Persisted, app-global preferences for the CODE font — the size and family of
// the code editor (Monaco) and the terminal (xterm), kept separate from the
// chrome/UI font (see lib/uiFontPreferences.ts).
//
// The chrome font rides a CSS custom property (`--ui-font-*`) because the whole
// rem-based UI reflows off the root font-size. Monaco and xterm can't: they're
// fixed-pixel canvas/DOM widgets that read an absolute px size + family once, at
// construction, and only re-measure when told to. So instead of a CSS variable
// this module exposes a tiny in-module pub/sub — `subscribeCodeFont` — that the
// write helpers fire after persisting. An already-mounted editor or terminal
// subscribes on mount and re-applies the new size/family imperatively
// (editor.updateOptions / term.options + refit) so a Settings change lands live
// without a reload or reconnect.

const SIZE_STORAGE_KEY = "omnigent:code-font-size";
const FAMILY_STORAGE_KEY = "omnigent:code-font-family";

// Code widgets read smaller than the chrome by convention, and a monospaced
// grid tolerates a wider useful range than body text — hence bounds distinct
// from the UI font's 12–20.
export const CODE_FONT_SIZE_DEFAULT = 13;
export const CODE_FONT_SIZE_MIN = 10;
export const CODE_FONT_SIZE_MAX = 24;
export const CODE_FONT_SIZE_STEP = 1;

/** Empty string = "editor default": no override, falls back to the mono stack. */
export const CODE_FONT_FAMILY_DEFAULT = "";

/** Longest family name we'll accept — a guard against a corrupt/oversized entry. */
const CODE_FONT_FAMILY_MAX_LENGTH = 100;

/**
 * The monospaced stack a code widget falls back to when no custom family is
 * set. Matches the app's default terminal/editor look (Geist Mono, then the
 * platform mono fonts) rather than a browser's generic monospace.
 */
export const CODE_FONT_FAMILY_FALLBACK =
  "'Geist Mono Variable', ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace";

/** Clamp an arbitrary number into the supported px range. */
export function clampCodeFontSizePx(px: number): number {
  return Math.min(CODE_FONT_SIZE_MAX, Math.max(CODE_FONT_SIZE_MIN, Math.round(px)));
}

function isValidPx(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Read the persisted code font size in px.
 *
 * Returns the default when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/malformed — never throws, so a corrupt
 * entry can't break app boot. A stored value outside the range is clamped.
 */
export function readCodeFontSizePx(): number {
  if (typeof window === "undefined") return CODE_FONT_SIZE_DEFAULT;
  try {
    const raw = window.localStorage.getItem(SIZE_STORAGE_KEY);
    if (!raw) return CODE_FONT_SIZE_DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    if (!isValidPx(parsed)) return CODE_FONT_SIZE_DEFAULT;
    return clampCodeFontSizePx(parsed);
  } catch {
    return CODE_FONT_SIZE_DEFAULT;
  }
}

/**
 * Persist the code font size (px), clamped to the supported range, then notify
 * subscribers so mounted editors/terminals re-apply it live. Swallows
 * quota/access errors so a failed write can't break the app.
 */
export function writeCodeFontSizePx(px: number): void {
  const sizePx = clampCodeFontSizePx(px);
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(SIZE_STORAGE_KEY, JSON.stringify(sizePx));
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  }
  // Broadcast the intended value, not a storage re-read: if the write above
  // failed (quota/denied), mounted editors/terminals must still re-font to the
  // new size now rather than snapping back to the stale/default stored value.
  emit({ sizePx, family: readCodeFontFamily() });
}

/**
 * Normalize a raw family name into a value safe to persist and to hand a code
 * widget: trimmed, with characters that could terminate/inject a CSS
 * declaration (`;{}` and control chars) stripped. Over-long input collapses to
 * the default. Returns "" for anything that isn't a usable family, so callers
 * treat empty as "editor default".
 */
function normalizeCodeFontFamily(value: unknown): string {
  if (typeof value !== "string") return CODE_FONT_FAMILY_DEFAULT;
  // eslint-disable-next-line no-control-regex -- intentionally stripping control chars
  const cleaned = value.replace(/[;{}\x00-\x1f\x7f]/g, "").trim();
  if (!cleaned || cleaned.length > CODE_FONT_FAMILY_MAX_LENGTH) {
    return CODE_FONT_FAMILY_DEFAULT;
  }
  return cleaned;
}

/**
 * Read the persisted code font family.
 *
 * Returns "" (editor default) when nothing is stored, on a server render (no
 * `window`), or when the stored value is missing/malformed — never throws, so a
 * corrupt entry can't break app boot.
 */
export function readCodeFontFamily(): string {
  if (typeof window === "undefined") return CODE_FONT_FAMILY_DEFAULT;
  try {
    const raw = window.localStorage.getItem(FAMILY_STORAGE_KEY);
    if (!raw) return CODE_FONT_FAMILY_DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    return normalizeCodeFontFamily(parsed);
  } catch {
    return CODE_FONT_FAMILY_DEFAULT;
  }
}

/**
 * Persist the code font family, then notify subscribers so mounted
 * editors/terminals re-apply it live. An empty (or all-stripped) name clears
 * the preference — reverting to the editor default — rather than storing a
 * blank. Swallows quota/access errors so a failed write can't break the app.
 */
export function writeCodeFontFamily(name: string): void {
  const family = normalizeCodeFontFamily(name);
  if (typeof window !== "undefined") {
    try {
      if (family) {
        window.localStorage.setItem(FAMILY_STORAGE_KEY, JSON.stringify(family));
      } else {
        window.localStorage.removeItem(FAMILY_STORAGE_KEY);
      }
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  }
  // Broadcast the intended value, not a storage re-read: a failed write must
  // still live-apply the new family to mounted editors/terminals.
  emit({ sizePx: readCodeFontSizePx(), family });
}

/**
 * Resolve a persisted family into the `fontFamily` value handed to a code
 * widget. A custom name gets the mono fallback stack appended (so an
 * uninstalled/partial name degrades to the app mono, not a serif); an empty
 * name resolves to the fallback stack itself. Both Monaco and the terminal use
 * this directly, so with no custom family they share one default look — the app
 * mono stack — rather than each falling back to its own built-in default.
 */
export function codeFontFamilyForEditor(family: string): string {
  const normalized = normalizeCodeFontFamily(family);
  return normalized ? `${normalized}, ${CODE_FONT_FAMILY_FALLBACK}` : CODE_FONT_FAMILY_FALLBACK;
}

/** The current code font size + family, read together for widget construction. */
export interface CodeFont {
  /** Font size in px, already clamped to the supported range. */
  sizePx: number;
  /** Custom family, or "" for the editor/terminal default. */
  family: string;
}

/** Read both code font preferences at once. Handy on editor/terminal mount. */
export function readCodeFont(): CodeFont {
  return { sizePx: readCodeFontSizePx(), family: readCodeFontFamily() };
}

type CodeFontListener = (font: CodeFont) => void;

const listeners = new Set<CodeFontListener>();

/**
 * Subscribe to code font changes. The callback fires with the current
 * {@link CodeFont} whenever the size or family is written (e.g. from Settings),
 * letting an already-mounted editor or terminal re-apply the change live —
 * these fixed-pixel widgets can't ride a CSS variable the way the chrome font
 * does. Returns an unsubscribe function.
 */
export function subscribeCodeFont(listener: CodeFontListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Notify subscribers of the given code font. Called after every write. */
function emit(font: CodeFont): void {
  for (const listener of listeners) listener(font);
}
