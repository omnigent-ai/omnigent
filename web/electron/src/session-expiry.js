// Recovering the desktop window when its outer auth session expires — the
// HTTP-redirect path.
//
// A workspace-hosted Omnigent sits behind the Databricks SSO gate. Some
// deployments answer an expired session with a 3xx redirect to a login page
// (``/login``, ``/login/sso``, or ``/login.html``); this watches for that raw
// redirect and reloads the window so the gate can re-challenge.
//
// NOTE: a managed Databricks workspace bounces an expired session via a
// CLIENT-SIDE navigation, which webRequest never surfaces — so this seam does
// not fire there. That case is handled by the away-watch's navigation
// detection (see silentReauthManaged in main.js), which is the primary recovery
// path. This HTTP-redirect seam remains for deployments that do emit a 3xx.
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
  // A managed workspace bounces an expired session to its login gate — observed
  // as `/login/sso` (and `/login`); some deployments use `/login.html`. Match
  // all of these. Scoped to the login path prefix so ordinary API redirects
  // (e.g. /ajax-api/…) are left alone.
  const p = pathname.toLowerCase();
  return (
    p === "/login" || p.startsWith("/login/") || p.endsWith("/login.html") || p === "login.html"
  );
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
