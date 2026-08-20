// Recovering the desktop window when its outer auth session expires.
//
// A workspace-hosted Omnigent sits behind the Databricks SSO gate. When that
// session's cookie lapses, the gate answers the SPA's API calls with a 303
// redirect to its own ``login.html`` instead of the expected JSON. The SPA
// can't parse the login page as data and dies on a "Failed to load: Fetch
// request failed due to expired user session" error — and a desktop user has
// no address bar to force a refresh out of it.
//
// The shell sees the raw redirect (independent of whichever server bundle is
// loaded), so it recovers here: on a login-page redirect for a connected
// server, reload the window. That re-issues the top-level navigation the SSO
// gate inspects, so it can re-challenge and re-mint the session.
//
// Kept Electron-free at its core (isLoginRedirect) so the matching logic is
// unit-testable (test/session-expiry.test.js) without booting the app.

/**
 * Whether a webRequest redirect is the auth gate bouncing an expired session
 * to its login page. Keyed on the redirect *target* pathname ending in
 * ``login.html`` — the one unambiguous signal from a real expired session
 * (see the module header). A same-origin API-to-API redirect, or any redirect
 * not landing on the login page, is left alone.
 *
 * @param {{ statusCode?: number, redirectURL?: string }} details A webRequest
 *   ``onBeforeRedirect`` detail object (or the fields it carries).
 * @returns {boolean}
 */
function isLoginRedirect(details) {
  const status = details?.statusCode ?? 0;
  if (status < 300 || status >= 400) return false;
  let pathname;
  try {
    pathname = new URL(details.redirectURL).pathname;
  } catch {
    return false;
  }
  return pathname.endsWith("/login.html") || pathname === "login.html";
}

/**
 * Wire expired-session recovery onto a session's redirect stream.
 *
 * Uses ``onBeforeRedirect`` — an observe-only event with no other listener in
 * this shell (Electron allows one listener per webRequest event per session,
 * and localhost_cors.js claims the others). On a login-page redirect whose
 * originating request targeted a connected server origin, the matching windows
 * are reloaded. Guarded to one reload per window between successful loads (via
 * the caller's ``reloadWindowsForOrigin``) so a persistently expired host does
 * not reload-loop.
 *
 * @param {Electron.Session} ses The session whose redirects to watch.
 * @param {(origin: string) => boolean} isConnectedServerOrigin Whether an
 *   origin belongs to a server some window is connected to.
 * @param {(origin: string) => void} reloadWindowsForOrigin Reload every window
 *   pinned to the given origin (the caller owns the once-per-window guard).
 */
function registerSessionExpiryReload(ses, isConnectedServerOrigin, reloadWindowsForOrigin) {
  ses.webRequest.onBeforeRedirect((details) => {
    if (!isLoginRedirect(details)) return;
    let origin;
    try {
      origin = new URL(details.url).origin;
    } catch {
      return;
    }
    if (!isConnectedServerOrigin(origin)) return;
    reloadWindowsForOrigin(origin);
  });
}

module.exports = {
  isLoginRedirect,
  registerSessionExpiryReload,
};
