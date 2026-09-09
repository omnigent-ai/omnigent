export const BACKGROUND_SESSION_TITLES_STORAGE_KEY = "omnigent:background-session-titles";
export const BACKGROUND_SESSION_TITLES_HEADER = "X-Omnigent-Background-Session-Titles";

export const DEFAULT_BACKGROUND_SESSION_TITLES_ENABLED = true;

export function readBackgroundSessionTitlesEnabled(): boolean {
  if (typeof window === "undefined") return DEFAULT_BACKGROUND_SESSION_TITLES_ENABLED;
  try {
    return window.localStorage.getItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY) !== "off";
  } catch {
    return DEFAULT_BACKGROUND_SESSION_TITLES_ENABLED;
  }
}

export function writeBackgroundSessionTitlesEnabled(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (enabled) {
      window.localStorage.removeItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY);
    } else {
      window.localStorage.setItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY, "off");
    }
  } catch {
    // localStorage failures should not make Settings unusable.
  }
}

export function backgroundSessionTitlesRequestHeaders(): Record<string, string> {
  return readBackgroundSessionTitlesEnabled() ? {} : { [BACKGROUND_SESSION_TITLES_HEADER]: "off" };
}
