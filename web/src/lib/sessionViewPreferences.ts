const STORAGE_KEY = "omnigent:default-session-view";

export const sessionViewDefaults = ["chat", "terminal"] as const;
export type DefaultSessionView = (typeof sessionViewDefaults)[number];

/** Preserve the existing behavior when no preference has been selected. */
export const SESSION_VIEW_DEFAULT: DefaultSessionView = "chat";

export function normalizeDefaultSessionView(value: string | null | undefined): DefaultSessionView {
  return value === "terminal" ? "terminal" : SESSION_VIEW_DEFAULT;
}

export function readDefaultSessionView(): DefaultSessionView {
  if (typeof window === "undefined") return SESSION_VIEW_DEFAULT;
  try {
    return normalizeDefaultSessionView(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return SESSION_VIEW_DEFAULT;
  }
}

export function writeDefaultSessionView(value: DefaultSessionView): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeDefaultSessionView(value);
    if (normalized === SESSION_VIEW_DEFAULT) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
