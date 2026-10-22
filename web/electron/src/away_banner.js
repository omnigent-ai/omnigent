// Detect when a shell window's main frame has navigated AWAY from its pinned
// server (e.g. an SSO redirect to an identity provider) and stayed away. Kept
// free of Electron imports so the behavior can be tested with a fake
// webContents — the same pattern as workspace-root-bounce.js.
//
// "Away" is origin-based, so it covers every subpage of the server (a
// Databricks ``…/omnigent`` mount with query args included): any committed
// main-frame URL on the pinned origin counts as "back", and the last such
// URL is remembered so the banner can offer to return to the exact page the
// user was on. Same-origin AUTH-GATE pages (the platform login page, the
// front-door OIDC namespace) count as away and are never recorded as return
// targets — otherwise the banner would offer to "return" to the login page
// the user is stuck behind.

"use strict";

/** How long the window must stay off the pinned origin before we offer to return. */
const AWAY_BANNER_DELAY_MS = 10_000;

/** Return a URL's origin, or null when it is not an absolute URL. */
function originOf(rawUrl) {
  try {
    return new URL(rawUrl).origin;
  } catch {
    return null;
  }
}

/**
 * Whether a URL on the pinned origin is an auth-gate page rather than app
 * content. Covers the Databricks workspace login page (which redirects an
 * expired session to ``/login.html?next_url=…`` on the SAME origin — see
 * session-expiry.js) and the Apps front-door OIDC namespace (``/oidc/``,
 * ``/.auth/`` — see runner/_entry.py). The app's own ``/auth/*`` flow is
 * deliberately NOT listed: those pages belong to the app and bounce quickly.
 *
 * @param {string} rawUrl
 * @returns {boolean}
 */
function isAuthGatePath(rawUrl) {
  let pathname;
  try {
    pathname = new URL(rawUrl).pathname;
  } catch {
    return false;
  }
  return (
    pathname === "/login.html" ||
    pathname === "/login" ||
    pathname.startsWith("/oidc/") ||
    pathname.startsWith("/.auth/")
  );
}

/**
 * Watch one shell window's main-frame navigations for "navigated away from
 * the pinned server and stayed away".
 *
 * - On every committed URL on the pinned origin, remember it as the return
 *   target (this is what makes subpages work) and report ``onReturn``.
 * - On a committed URL on a foreign origin, arm a timer. If the window is
 *   still foreign when it fires, report ``onAway`` once. Foreign commits
 *   restart the timer, so a long multi-hop SSO flow only triggers the banner
 *   after the flow settles. After ``onAway`` fires, further foreign commits
 *   do not re-report (the user may have dismissed the banner); an on-server
 *   commit or an explicit ``reset()`` re-arms the watch.
 * - While the window is unpinned (setup page), the watch is inert.
 *
 * @param {{
 *   on: (event: string, listener: (...args: unknown[]) => void) => void,
 *   getURL: () => string
 * }} webContents
 * @param {{
 *   getPinnedOrigin: () => string | null,
 *   onAway: (returnUrl: string | null) => void,
 *   onReturn: () => void,
 *   delayMs?: number,
 *   debugLog?: (message: string) => void,
 *   setTimeoutFn?: typeof setTimeout,
 *   clearTimeoutFn?: typeof clearTimeout
 * }} options
 * @returns {{ dispose: () => void, reset: () => void }}
 */
function registerServerAwayWatch(
  webContents,
  {
    getPinnedOrigin,
    onAway,
    onReturn,
    delayMs = AWAY_BANNER_DELAY_MS,
    debugLog = () => {},
    setTimeoutFn = setTimeout,
    clearTimeoutFn = clearTimeout,
  },
) {
  let lastServerUrl = null;
  let awayTimer = null;
  let notified = false;
  let disposed = false;

  function cancelTimer() {
    if (awayTimer !== null) {
      clearTimeoutFn(awayTimer);
      awayTimer = null;
    }
  }

  function onServer(url) {
    lastServerUrl = url;
    notified = false;
    cancelTimer();
    onReturn();
  }

  function onForeign() {
    if (notified) return;
    cancelTimer();
    debugLog(`away-watch: left the pinned server, banner in ${delayMs}ms unless it returns`);
    awayTimer = setTimeoutFn(() => {
      awayTimer = null;
      // Re-verify at fire time: a load failure or an eventless state change
      // (unpin, server switch) must not leave a stale banner.
      const pinned = getPinnedOrigin();
      const current = webContents.getURL();
      if (!pinned || (originOf(current) === pinned && !isAuthGatePath(current))) return;
      notified = true;
      debugLog(`away-watch: still away after ${delayMs}ms, showing the return banner`);
      onAway(lastServerUrl);
    }, delayMs);
  }

  function handleCommit(url) {
    if (disposed) return;
    const pinned = getPinnedOrigin();
    if (!pinned) {
      // Unpinned (setup page, or an unpin mid-flow): nothing to return to.
      lastServerUrl = null;
      notified = false;
      cancelTimer();
      onReturn();
      return;
    }
    if (originOf(url) === pinned && !isAuthGatePath(url)) onServer(url);
    else onForeign();
  }

  const onDidNavigate = (_event, url) => handleCommit(url);
  const onDidNavigateInPage = (_event, url, isMainFrame) => {
    if (isMainFrame) handleCommit(url);
  };
  webContents.on("did-navigate", onDidNavigate);
  webContents.on("did-navigate-in-page", onDidNavigateInPage);

  return {
    /** Stop watching (window closed); never reports again. */
    dispose() {
      disposed = true;
      cancelTimer();
    },
    /**
     * Re-arm after an explicit return attempt: the load may land on the
     * SSO flow again (expired session), and that new away episode must be
     * allowed to notify.
     */
    reset() {
      notified = false;
      cancelTimer();
    },
  };
}

module.exports = { AWAY_BANNER_DELAY_MS, isAuthGatePath, registerServerAwayWatch };
