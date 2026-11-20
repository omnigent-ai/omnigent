"use strict";

// Sessions popped out into their own desktop window ("Open in new window").
// Pure and Electron-free so `node --test` can exercise it directly.

/** Session ids the page may ask to pop out; anything else is refused. */
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;

/** A popped-out window's size: room for one chat, smaller than the main window. */
const SESSION_WINDOW_SIZE = Object.freeze({ width: 960, height: 820 });

/** Query the SPA reads to start a popped-out window with its sidebar collapsed. */
const SESSION_WINDOW_QUERY = "window=session";

/**
 * The route a popped-out window opens at, or null for an id the page must not pass.
 *
 * @param {unknown} sessionId
 * @returns {string | null}
 */
function sessionWindowPath(sessionId) {
  return typeof sessionId === "string" && SESSION_ID.test(sessionId)
    ? `/c/${sessionId}?${SESSION_WINDOW_QUERY}`
    : null;
}

/**
 * `pageUrl` without the pop-out marker, so a New Window cloned from a pop-out opens as a
 * normal window. Other query parameters and unparsable URLs pass through unchanged.
 *
 * @param {string | undefined} pageUrl
 * @returns {string | undefined}
 */
function withoutSessionWindowQuery(pageUrl) {
  let url;
  try {
    url = new URL(pageUrl ?? "");
  } catch {
    return pageUrl;
  }
  if (url.searchParams.get("window") !== "session") return pageUrl;
  url.searchParams.delete("window");
  return url.toString();
}

/**
 * Whether a pop-out has left `sessionId`: for another page on its server, or for a bundled
 * page such as the connect-failure screen. A window still loading (blank) or signing in
 * (on the identity provider's site) has not left it.
 *
 * @param {string} pageUrl
 * @param {string} serverUrl
 * @param {string} sessionId
 * @returns {boolean}
 */
function leftSession(pageUrl, serverUrl, sessionId) {
  if (pageUrl === "" || pageUrl === "about:blank") return false;
  let page;
  let server;
  try {
    page = new URL(pageUrl);
    server = new URL(serverUrl);
  } catch {
    return true;
  }
  if (page.protocol !== "https:" && page.protocol !== "http:") return true;
  if (page.origin !== server.origin) return false;
  return !page.pathname.replace(/\/+$/, "").endsWith(`/c/${sessionId}`);
}

/**
 * The open window already popped out for this server's session, so opening it again
 * focuses that window instead of stacking a duplicate, even before its page has loaded.
 *
 * @template {{isDestroyed(): boolean, webContents: {getURL(): string}}} W
 * @param {Map<W, {serverUrl?: string | null, sessionWindowId?: string}>} windows
 * @param {{serverUrl: string, sessionId: string}} target
 * @returns {W | null}
 */
function findSessionWindow(windows, { serverUrl, sessionId }) {
  for (const [win, state] of windows) {
    if (win.isDestroyed()) continue;
    if (state.sessionWindowId !== sessionId || state.serverUrl !== serverUrl) continue;
    if (!leftSession(win.webContents.getURL(), serverUrl, sessionId)) return win;
  }
  return null;
}

module.exports = {
  SESSION_WINDOW_SIZE,
  findSessionWindow,
  sessionWindowPath,
  withoutSessionWindowQuery,
};
