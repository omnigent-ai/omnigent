// Persisted, per-device preference for letting the chat column (transcript,
// composer, and indicators) and assistant prose span the full pane width
// instead of the centered reading column.
//
// Applied as a `data-full-width-chat` attribute on <html> so index.css can
// override the column and prose width variables without threading React state
// through every chat surface.

const STORAGE_KEY = "omnigent:full-width-chat";

export const DEFAULT_FULL_WIDTH_CHAT = false;

/**
 * Read the persisted full-width chat preference. Returns the default (off)
 * when nothing is stored, on a server render, or when the stored value is
 * malformed — never throws.
 */
export function readFullWidthChat(): boolean {
  if (typeof window === "undefined") return DEFAULT_FULL_WIDTH_CHAT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_FULL_WIDTH_CHAT;
    return raw === "true";
  } catch {
    return DEFAULT_FULL_WIDTH_CHAT;
  }
}

/** Persist the full-width chat preference. Swallows quota/access errors. */
export function writeFullWidthChat(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/** Reflect the preference on <html> so the CSS overrides take effect. */
export function applyFullWidthChat(value: boolean): void {
  if (typeof document === "undefined") return;
  if (value) document.documentElement.dataset.fullWidthChat = "true";
  else delete document.documentElement.dataset.fullWidthChat;
}
