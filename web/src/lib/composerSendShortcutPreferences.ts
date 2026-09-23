export const COMPOSER_SEND_SHORTCUT_STORAGE_KEY = "omnigent:composer-submit-with-mod-enter";

export const DEFAULT_SUBMIT_WITH_MOD_ENTER = false;

interface ComposerSendKeyEvent {
  key: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  isComposing?: boolean;
}

export function parseSubmitWithModEnter(value: unknown): boolean {
  return value === "true";
}

export function readSubmitWithModEnter(): boolean {
  if (typeof window === "undefined") return DEFAULT_SUBMIT_WITH_MOD_ENTER;
  try {
    return parseSubmitWithModEnter(window.localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY));
  } catch {
    return DEFAULT_SUBMIT_WITH_MOD_ENTER;
  }
}

export function writeSubmitWithModEnter(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (value === DEFAULT_SUBMIT_WITH_MOD_ENTER) {
      window.localStorage.removeItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY);
    } else {
      window.localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    }
  } catch {
    // A storage failure must not make the composer unusable.
  }
}

export function isComposerSendKey(
  event: ComposerSendKeyEvent,
  submitWithModEnter: boolean,
  isMobile: boolean,
): boolean {
  if (isMobile || event.key !== "Enter" || event.isComposing || event.shiftKey || event.altKey) {
    return false;
  }

  const hasMod = event.metaKey === true || event.ctrlKey === true;
  return submitWithModEnter ? hasMod : true;
}

/**
 * The "steer everything now" chord: Mod+Enter (Cmd on macOS, Ctrl elsewhere).
 * When Mod+Enter is already the send chord, Mod+Shift+Enter takes over so the
 * modifier keeps one meaning per mode.
 */
export function isComposerSteerAllKey(
  event: ComposerSendKeyEvent,
  submitWithModEnter: boolean,
  isMobile: boolean,
): boolean {
  if (isMobile || event.key !== "Enter" || event.isComposing || event.altKey) return false;
  const hasMod = event.metaKey === true || event.ctrlKey === true;
  if (!hasMod) return false;
  return submitWithModEnter ? event.shiftKey === true : event.shiftKey !== true;
}
