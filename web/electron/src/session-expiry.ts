// Recovering embedded-auth connections when their outer auth session expires.
//
// A workspace-hosted Omnigent sits behind the Databricks SSO gate. Some
// deployments answer an expired session with a 3xx redirect to a login page
// (``/login``, ``/login/sso``, or ``/login.html``); this watches for that raw
// redirect and reloads the window so the gate can re-challenge.
//
// Databricks browser-auth connections are excluded by the caller. Their cookie
// lifecycle and request guard live in databricks-auth.js, never embedded SSO.
//
// Kept Electron-free at its core (isLoginRedirect) so the matching logic is
// unit-testable (test/session-expiry.test.ts) without booting the app.

/** The fields of a webRequest ``onBeforeRedirect`` detail object that matter here. */
export interface LoginRedirectDetails {
  statusCode?: number;
  redirectURL?: string;
}

/** A redirect observed on a session, with the URL of the request that was redirected. */
export interface SessionRedirectDetails extends LoginRedirectDetails {
  url: string;
}

/** The part of an Electron session whose redirects are watched. */
export interface RedirectWatchSession {
  webRequest: {
    onBeforeRedirect: (listener: (details: SessionRedirectDetails) => void) => void;
  };
}

/**
 * Whether a webRequest redirect is the auth gate bouncing an expired session
 * to its login page. Keyed on the redirect *target* pathname ending in
 * ``login.html`` — the one unambiguous signal from a real expired session
 * (see the module header). A same-origin API-to-API redirect, or any redirect
 * not landing on the login page, is left alone.
 */
export function isLoginRedirect(details: LoginRedirectDetails | null | undefined): boolean {
  const status = details?.statusCode ?? 0;
  if (status < 300 || status >= 400) return false;
  let pathname: string;
  try {
    // A missing URL stringifies to "undefined", which does not parse either.
    pathname = new URL(String(details?.redirectURL)).pathname;
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
 * and localhost_cors.ts claims the others). On a login-page redirect whose
 * originating request targeted a connected server origin, the matching windows
 * are reloaded. Guarded to one reload per window between successful loads (via
 * the caller's ``reloadWindowsForOrigin``) so a persistently expired host does
 * not reload-loop.
 *
 * @param ses The session whose redirects to watch.
 * @param isConnectedServerOrigin Whether an origin belongs to a server some
 *   window is connected to.
 * @param reloadWindowsForOrigin Reload every window pinned to the given origin
 *   (the caller owns the once-per-window guard).
 */
export function registerSessionExpiryReload(
  ses: RedirectWatchSession,
  isConnectedServerOrigin: (origin: string) => boolean,
  reloadWindowsForOrigin: (origin: string) => void,
): void {
  ses.webRequest.onBeforeRedirect((details) => {
    if (!isLoginRedirect(details)) return;
    let origin: string;
    try {
      origin = new URL(details.url).origin;
    } catch {
      return;
    }
    if (!isConnectedServerOrigin(origin)) return;
    reloadWindowsForOrigin(origin);
  });
}
