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

"use strict";

const { joinServerUrl, workspaceIdentityKey } = require("./url");

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
 * The redirect's ``webContentsId`` is passed through so the caller can
 * attribute the event to the window whose webContents issued the request —
 * the session is app-global, so without attribution a login-shaped redirect
 * from ANY contents (another window on the same host, or a hostile embedded
 * page) would reload every identity-matching window.
 *
 * @param {Electron.Session} ses The session whose redirects to watch.
 * @param {(origin: string) => boolean} isConnectedServerOrigin Whether an
 *   origin belongs to a server some window is connected to.
 * @param {(origin: string, webContentsId: number | undefined) => void}
 *   reloadWindowsForOrigin Reload the matching window (the caller owns both
 *   the webContents attribution and the once-per-window guard).
 */
function registerSessionExpiryReload(ses, isConnectedServerOrigin, reloadWindowsForOrigin) {
  ses.webRequest.onBeforeRedirect((details) => {
    if (!isLoginRedirect(details)) return;
    const identity = workspaceIdentityKey(details.url);
    if (!identity || !isConnectedServerOrigin(identity)) return;
    reloadWindowsForOrigin(identity, details.webContentsId);
  });
}

/**
 * Whether a window's pinned workspace identity is hit by an expired-session
 * redirect on the given request identity. The redirected API request rarely
 * carries Databricks' ``?o=`` workspace selector even when the window's
 * pinned identity does — the selector lives in the SPA's URL, not in every
 * API call — so a bare-origin request matches every pinned identity on that
 * origin. A request that DOES carry a selector still requires the exact
 * identity, so multi-workspace hosts reload only the named workspace.
 *
 * @param {string | null | undefined} windowIdentity A window's pinned
 *   workspaceIdentityKey value.
 * @param {string | null | undefined} requestIdentity workspaceIdentityKey of
 *   the redirected request's URL.
 * @returns {boolean}
 */
function expiredRequestMatchesIdentity(windowIdentity, requestIdentity) {
  if (!windowIdentity || !requestIdentity) return false;
  if (windowIdentity === requestIdentity) return true;
  return !requestIdentity.includes("?") && windowIdentity.startsWith(`${requestIdentity}?`);
}

/**
 * Whether a main-frame destination is the pinned server's OIDC login route.
 * The exact pathname must match; the identity is matched with the same rule
 * as the redirect stream (expiredRequestMatchesIdentity): a selector-less
 * destination — the SPA's `/auth/login` assignment usually drops the `?o=`
 * query — still intercepts on a `?o=`-pinned window, while a destination
 * carrying a DIFFERENT selector never does. Query parameters such as the
 * SPA's `return_to` are allowed — but a `return_to` whose own workspace
 * identity conflicts with the window's is a deliberate cross-workspace login
 * navigation, not this window's expiry, and must pass through untouched.
 *
 * @param {string} destinationUrl
 * @param {string | null | undefined} serverUrl
 * @returns {boolean}
 */
function isOidcLoginNavigation(destinationUrl, serverUrl) {
  if (!serverUrl) return false;
  try {
    const destination = new URL(destinationUrl);
    const expected = new URL(joinServerUrl(serverUrl, "/auth/login"));
    if (destination.pathname !== expected.pathname) return false;
    const windowIdentity = workspaceIdentityKey(serverUrl);
    if (!expiredRequestMatchesIdentity(windowIdentity, workspaceIdentityKey(destinationUrl))) {
      return false;
    }
    // A conflicting return_to identity is a deliberate cross-workspace login,
    // not expiry recovery for this window.
    const returnTo = destination.searchParams.get("return_to");
    if (returnTo !== null) {
      let returnIdentity = null;
      try {
        returnIdentity = workspaceIdentityKey(new URL(returnTo, destination).toString());
      } catch {
        // A malformed return_to carries no signal that should defeat recovery.
      }
      if (returnIdentity && !expiredRequestMatchesIdentity(windowIdentity, returnIdentity)) {
        return false;
      }
    }
    return true;
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
      if (workspaceIdentityKey(current.toString()) === workspaceIdentityKey(serverUrl)) {
        returnUrl = current.toString();
      }
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
  expiredRequestMatchesIdentity,
  isLoginRedirect,
  isOidcLoginNavigation,
  registerSessionExpiryReload,
  registerOidcSessionExpiryHandoff,
};
