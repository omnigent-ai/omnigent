// View → Open in Browser: hand the window's current page to the default
// browser. Only a page on the server the window is pinned to qualifies, so the
// setup, connection and error pages (file: or another origin) are never opened.
//
// Pure + dep-free so `node --test` can exercise it without Electron.

"use strict";

/**
 * The URL to open in the browser for a window, or null when it has none.
 *
 * @param {string | null | undefined} currentUrl The window's top-level URL.
 * @param {string | null | undefined} pinnedOrigin The server origin the window
 *   is pinned to, e.g. ``"http://127.0.0.1:6767"``.
 * @returns {string | null}
 */
function browserUrlFor(currentUrl, pinnedOrigin) {
  if (!currentUrl || !pinnedOrigin) return null;
  let parsed;
  try {
    parsed = new URL(currentUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  return parsed.origin === pinnedOrigin ? parsed.href : null;
}

module.exports = { browserUrlFor };
