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
 * Whether `pageUrl` still shows `sessionId` (the user may have navigated the window away).
 *
 * @param {string} pageUrl
 * @param {string} sessionId
 * @returns {boolean}
 */
function showsSession(pageUrl, sessionId) {
  try {
    return new URL(pageUrl).pathname.replace(/\/+$/, "").endsWith(`/c/${sessionId}`);
  } catch {
    return false;
  }
}

/**
 * The open window already popped out for this server's session, so opening it again
 * focuses that window instead of stacking a duplicate.
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
    if (showsSession(win.webContents.getURL(), sessionId)) return win;
  }
  return null;
}

module.exports = {
  SESSION_WINDOW_SIZE,
  findSessionWindow,
  sessionWindowPath,
};
