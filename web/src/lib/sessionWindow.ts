// A session popped out into its own window ("Open in new window"). The window
// opens at `/c/<id>?window=session`; that query marks it at load so it keeps the
// desktop layout at its smaller size and starts with the sidebar collapsed.

import { withBasePath } from "@/lib/basePath";

/** Query parameter and value a popped-out session window opens with. */
export const SESSION_WINDOW_PARAM = "window";
export const SESSION_WINDOW_VALUE = "session";

/** Size of a browser pop-out; the desktop shell sizes its own windows. */
export const SESSION_WINDOW_FEATURES = "popup,width=960,height=820";

/** The in-app route a popped-out window opens at. */
export function sessionWindowPath(sessionId: string): string {
  return `/c/${encodeURIComponent(sessionId)}?${SESSION_WINDOW_PARAM}=${SESSION_WINDOW_VALUE}`;
}

/**
 * Whether this page was opened as a session window: the query is present and the
 * page is a real pop-out (the desktop shell's, or a browser popup with an opener).
 * A copied pop-out link opened elsewhere, such as on a phone, keeps its usual layout.
 */
export function startsAsSessionWindow(): boolean {
  if (typeof window === "undefined") return false;
  const params = new URLSearchParams(window.location.search);
  if (params.get(SESSION_WINDOW_PARAM) !== SESSION_WINDOW_VALUE) return false;
  const desktop = (window as { omnigentDesktop?: { kind?: string } }).omnigentDesktop;
  return desktop?.kind === "electron" || window.opener != null;
}

/** Set once before first paint by `applyDesktopAppAttribute`. */
export function isSessionWindow(): boolean {
  return (
    typeof document !== "undefined" && document.documentElement.dataset.sessionWindow === "true"
  );
}

/** Whether `popup` already shows `target`; false while blank or if it left this origin. */
function popupShows(popup: Window, target: string): boolean {
  try {
    return popup.location.pathname === new URL(target, window.location.origin).pathname;
  } catch {
    return false;
  }
}

/**
 * Open `sessionId` in a browser popup. Its window name finds a popup already open
 * for the session, which comes forward without reloading.
 */
export function openBrowserSessionWindow(sessionId: string): boolean {
  const target = withBasePath(sessionWindowPath(sessionId));
  const popup = window.open("", `omnigent-session-${sessionId}`, SESSION_WINDOW_FEATURES);
  if (!popup) return false;
  if (!popupShows(popup, target)) popup.location.href = target;
  popup.focus();
  return true;
}
