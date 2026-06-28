// Persisted, app-global preference for the interface font.
//
// This is a *preference*, not per-session state: the chosen font applies to
// the whole UI (chat, sidebar, settings) and should survive a page refresh.
// It is stored under a single localStorage key, mirroring the global-toggle
// semantics of fileViewPreferences.ts.
//
// The font is applied by toggling a `data-chat-font` attribute on the <html>
// element, which CSS in index.css keys off of. We deliberately mirror the
// next-themes approach (a single attribute on the document root) rather than
// rewriting the `--font-sans` custom property: Tailwind v4 inlines the
// `font-sans` utility's value at build time from the `@theme inline` block, so
// reassigning the variable at runtime would not retroactively change utility
// classes. A document-level font-family override does.

export const chatFonts = ["system", "geist"] as const;

export type ChatFont = (typeof chatFonts)[number];

const STORAGE_KEY = "omnigent:chat-font";

/** The native system stack — Omnigent's default; renders as native chrome. */
export const DEFAULT_CHAT_FONT: ChatFont = "system";

/** Human-readable labels for the settings UI. */
export const CHAT_FONT_LABELS: Record<ChatFont, string> = {
  system: "System",
  geist: "Geist",
};

/** Whether a string is one of the selectable fonts. */
export function isChatFont(value: string | null | undefined): value is ChatFont {
  return value === "system" || value === "geist";
}

/**
 * Read the persisted font preference. Returns the default when nothing is
 * stored, on a server render (no `window`), or when the stored value is
 * unrecognized — never throws, so a corrupt entry can't break the app.
 */
export function readChatFont(): ChatFont {
  if (typeof window === "undefined") return DEFAULT_CHAT_FONT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return isChatFont(raw) ? raw : DEFAULT_CHAT_FONT;
  } catch {
    return DEFAULT_CHAT_FONT;
  }
}

/**
 * Persist the font preference and apply it. Swallows quota/access errors so a
 * failed write can't break the app — the in-DOM effect still happens.
 */
export function writeChatFont(font: ChatFont): void {
  applyChatFont(font);
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, font);
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/**
 * Reflect the chosen font onto the document root. The default ("system")
 * removes the attribute so the base `font-sans` stack applies; any non-default
 * font sets `data-chat-font` for the index.css override to match.
 */
export function applyChatFont(font: ChatFont): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (font === DEFAULT_CHAT_FONT) {
    root.removeAttribute("data-chat-font");
  } else {
    root.setAttribute("data-chat-font", font);
  }
}

/**
 * Apply the persisted font at boot. Called from main.tsx before first paint so
 * a non-default choice doesn't flash the system font first.
 */
export function initChatFont(): void {
  applyChatFont(readChatFont());
}
