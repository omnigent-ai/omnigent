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
 * Identify marked Electron windows and browser pop-ups with an opener.
 * Copied links keep the usual layout.
 */
export function startingSessionWindowId(): string | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  if (params.get(SESSION_WINDOW_PARAM) !== SESSION_WINDOW_VALUE) return null;
  const desktop = (window as { omnigentDesktop?: { kind?: string } }).omnigentDesktop;
  if (desktop?.kind !== "electron" && !window.opener) return null;
  const match = /\/c\/([^/]+)\/?$/.exec(window.location.pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

/** Whether this page was opened as a session window; see `startingSessionWindowId`. */
export function startsAsSessionWindow(): boolean {
  return startingSessionWindowId() !== null;
}

/** Set once before first paint by `applyDesktopAppAttribute`. */
export function isSessionWindow(): boolean {
  return (
    typeof document !== "undefined" && document.documentElement.dataset.sessionWindow === "true"
  );
}

/**
 * Whether opening `sessionId` would only point this pop-out at itself: it was popped
 * out for that session and still shows it (`shownSessionId`). After the pop-out moves
 * to another session, its original session can be popped out again.
 */
export function isOwnSessionWindow(sessionId: string, shownSessionId: string | undefined): boolean {
  if (!isSessionWindow()) return false;
  const own = document.documentElement.dataset.sessionWindowFor;
  return own === sessionId && shownSessionId === sessionId;
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
  const name = `omnigent-session-${sessionId}`;
  // A pop-out that moved to another session would otherwise resolve its own stale name.
  if (window.name === name && !popupShows(window, target)) window.name = "";
  const popup = window.open("", name, SESSION_WINDOW_FEATURES);
  if (!popup) return false;
  if (!popupShows(popup, target)) popup.location.href = target;
  popup.focus();
  return true;
}
