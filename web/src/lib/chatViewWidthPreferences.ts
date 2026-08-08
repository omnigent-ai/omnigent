// Persisted, app-global preference for the maximum width of the chat column.

const STORAGE_KEY = "omnigent:chat-view-width";
const CSS_VARIABLE = "--chat-column-max-width";

export const chatViewWidths = ["normal", "wide", "extra-wide"] as const;
export type ChatViewWidth = (typeof chatViewWidths)[number];

export const CHAT_VIEW_WIDTH_DEFAULT: ChatViewWidth = "normal";

const CHAT_VIEW_WIDTH_MAX: Record<ChatViewWidth, string> = {
  normal: "48rem",
  wide: "56rem",
  "extra-wide": "64rem",
};

export function isChatViewWidth(value: string | null | undefined): value is ChatViewWidth {
  return value === "normal" || value === "wide" || value === "extra-wide";
}

export function normalizeChatViewWidth(value: string | null | undefined): ChatViewWidth {
  return isChatViewWidth(value) ? value : CHAT_VIEW_WIDTH_DEFAULT;
}

export function readChatViewWidth(): ChatViewWidth {
  if (typeof window === "undefined") return CHAT_VIEW_WIDTH_DEFAULT;
  try {
    return normalizeChatViewWidth(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return CHAT_VIEW_WIDTH_DEFAULT;
  }
}

export function applyChatViewWidth(value: ChatViewWidth): void {
  if (typeof document === "undefined") return;
  document.documentElement.style.setProperty(CSS_VARIABLE, CHAT_VIEW_WIDTH_MAX[value]);
}

export function writeChatViewWidth(value: ChatViewWidth): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeChatViewWidth(value);
  applyChatViewWidth(normalized);
  try {
    if (normalized === CHAT_VIEW_WIDTH_DEFAULT) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage access errors shouldn't break settings.
  }
}
