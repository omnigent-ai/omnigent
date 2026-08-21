// Recovering the desktop window when its auth session expires.
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
// Self-hosted OIDC has a second expiry signal: the SPA assigns the main frame
// to the same-server `/auth/login` route after an API 401. The shell must stop
// that navigation before it can redirect to a third-party IdP, reauthenticate
// in the system browser, and restore the exact route the user was viewing.
//
// Kept Electron-free at its core so the matching logic and event wiring are
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

/**
 * Whether a main-frame destination is the pinned server's OIDC login route.
 * The origin and exact pathname must both match; query parameters such as the
 * SPA's `return_to` are allowed.
 *
 * @param {string} destinationUrl
 * @param {string | null | undefined} serverUrl
 * @returns {boolean}
 */
function isOidcLoginNavigation(destinationUrl, serverUrl) {
  if (!serverUrl) return false;
  try {
    const destination = new URL(destinationUrl);
    const server = new URL(serverUrl);
    const basePath = server.pathname.replace(/\/+$/, "");
    return (
      destination.origin === server.origin && destination.pathname === `${basePath}/auth/login`
    );
  } catch {
    return false;
  }
}

/**
 * Stop live-renderer OIDC expiry navigation before the server can redirect the
 * main frame to an IdP. The callback owns browser authentication and route
 * restoration. Repeated navigation events share one in-flight callback.
 *
 * @param {Electron.WebContents} webContents
 * @param {() => string | null} serverUrlForWindow
 * @param {(params: { serverUrl: string, returnUrl: string }) => Promise<void>} onExpired
 */
function registerOidcSessionExpiryHandoff(webContents, serverUrlForWindow, onExpired) {
  let inFlight = null;
  const intercept = (event, destinationUrl, isMainFrame = true) => {
    if (isMainFrame === false) return;
    const serverUrl = serverUrlForWindow();
    if (!isOidcLoginNavigation(destinationUrl, serverUrl)) return;
    event.preventDefault();
    if (inFlight) return;

    let returnUrl = serverUrl;
    try {
      const current = new URL(webContents.getURL());
      if (current.origin === new URL(serverUrl).origin) returnUrl = current.toString();
    } catch {
      // Fall back to the clean server URL when the current page is unavailable.
    }
    inFlight = Promise.resolve(onExpired({ serverUrl, returnUrl }))
      .catch(() => {})
      .finally(() => {
        inFlight = null;
      });
  };

  webContents.on("will-navigate", (event, url) => intercept(event, url));
  webContents.on("will-redirect", (event, url, _isInPlace, isMainFrame) =>
    intercept(event, url, isMainFrame),
  );
}

module.exports = {
  isLoginRedirect,
  isOidcLoginNavigation,
  registerSessionExpiryReload,
  registerOidcSessionExpiryHandoff,
};
