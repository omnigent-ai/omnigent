// Deep-link decision logic for the desktop shell, kept PURE (no Electron) so
// it unit-tests without a BrowserWindow. main.js owns the wiring (ingestion
// from open-url / second-instance / argv, the queue, and the orchestrator
// that acts on these decisions); see README "Deep links".
//
// An `omnigent://<hostname>/c/<session_id>` URL names a server by host (with
// port if non-default) and a conversation by the SPA's own `/c/:id` route.
// The link carries no http/https scheme — we infer it with the SAME rule the
// setup page uses (defaultSchemeFor: http for loopback, https for remote),
// so a deep link and a pasted URL can never disagree on scheme. The
// workspace mount (`/ml/omnigents`) is deliberately NOT in the link: it is
// server-determined and discovered by expandDatabricksWorkspaceUrl, exactly
// as for a pasted workspace URL.

"use strict";

const { defaultSchemeFor } = require("./url");

/**
 * v1 accepted SPA path. `/c/<conversationId>` — a single path segment after
 * `/c/`, with an optional trailing slash. Anything else (other routes,
 * nested paths, empty id) is dropped silently: an unrecognized deep link
 * must never crash or mis-navigate, and the SPA's own router stays the
 * authority on what a valid conversation id is.
 */
const DEEP_LINK_PATH_RE = /^\/c\/[^/]+\/?$/;

/**
 * Parse an `omnigent://` deep link into a server origin + an in-app path.
 *
 * The origin is the http(s) origin inferred from the link's host (loopback →
 * http, else https), normalized via normalizeUrl. The path is the SPA
 * conversation route (`/c/<id>`), basename-less — the same shape the SPA
 * already emits for notification `navigatePath`, so the embedded
 * (workspace) build's `basenamedRouting` rebases it under the mount.
 *
 * @param {string} raw e.g. ``"omnigent://localhost:8000/c/conv_abc"``.
 * @returns {{ origin: string, path: string } | null} ``null`` for anything
 *   that isn't a valid `omnigent://.../c/<id>` link (wrong scheme, no host,
 *   non-`/c/` path, unparseable input).
 */
function parseOmnigentDeepLink(raw) {
  let url;
  try {
    url = new URL(String(raw ?? ""));
  } catch {
    return null;
  }
  if (url.protocol !== "omnigent:") return null;
  // No host → a bare `omnigent://` or `omnigent:`; nothing to connect to.
  if (url.host === "") return null;
  const path = url.pathname;
  if (!DEEP_LINK_PATH_RE.test(path)) return null;
  // Infer the http(s) scheme from the host the same way the setup page does,
  // so a deep link to `localhost` is http and to a remote host is https. The
  // origin is returned in `new URL(...).origin` form (NO trailing slash) so it
  // matches `originOf()` everywhere else in the shell — a trailing slash here
  // would make findKnownServerUrl/knownOrigins miss every known server and
  // force a wasteful network probe for already-connected workspaces.
  const scheme = defaultSchemeFor(url.host);
  let origin;
  try {
    origin = new URL(`${scheme}://${url.host}`).origin;
  } catch {
    return null;
  }
  return { origin, path };
}

/**
 * Decide how to open a deep link given a snapshot of the live windows and
 * the set of servers the user has previously trusted.
 *
 * The decision is the careful half of deep-link handling: a deep link is an
 * "open THIS on THAT server" intent, so we prefer reusing a window already
 * on that server (no second window, no dropped stream); fall back to
 * opening a new window on a server the user already chose; and require
 * explicit consent only for a server the user has NEVER connected to —
 * because pinning a new origin is a privilege grant (notifications, badge,
 * mic), and a clicked link must not silently pin an attacker-chosen origin.
 *
 * Each window snapshot carries:
 *   - `origin`: the origin the window is PINNED to (null = setup page);
 *   - `currentOrigin`: origin of the window's top-level page RIGHT NOW
 *     (may differ from `origin` mid-SSO redirect on a foreign IdP page).
 *
 * @param {Object} state
 * @param {string} state.targetOrigin The deep link's server origin.
 * @param {{origin: string | null, currentOrigin: string | null}[]} state.windows
 *   Live shell windows, in creation order.
 * @param {string[]} state.knownOrigins Origins the user previously connected
 *   to (origins of `recent_servers` ∪ {saved `server_url`}).
 * @param {number | null} [state.focusedIndex] Index into `windows` of the
 *   currently focused shell window, if any (preferred when several windows
 *   are pinned to the target origin).
 * @returns {{ strategy: "reuse-inplace", windowIndex: number }
 *   | { strategy: "reuse-reload", windowIndex: number }
 *   | { strategy: "open-known" }
 *   | { strategy: "consent-unknown" }}
 */
function chooseDeepLinkStrategy(state) {
  const { targetOrigin, windows, knownOrigins } = state;
  const focusedIndex = state.focusedIndex ?? null;

  // Windows pinned to the target origin — these already trust it.
  const pinned = windows.map((w, i) => ({ w, i })).filter(({ w }) => w.origin === targetOrigin);

  if (pinned.length > 0) {
    // Prefer a window currently ON the origin: its SPA router listener is
    // mounted, so we can navigate in-place without dropping the in-flight
    // stream OR disrupting another window's auth flow. Focus only breaks ties
    // AMONG on-origin windows (the user is looking at one of them). When every
    // pinned window is mid-auth (off-origin), reload — but still prefer the
    // focused one so a reload hits the window the user expects.
    const onOrigin = pinned.filter(({ w }) => w.currentOrigin === targetOrigin);
    if (onOrigin.length > 0) {
      const pick = onOrigin.find(({ i }) => i === focusedIndex) ?? onOrigin[0];
      return { strategy: "reuse-inplace", windowIndex: pick.i };
    }
    const pick = pinned.find(({ i }) => i === focusedIndex) ?? pinned[0];
    return { strategy: "reuse-reload", windowIndex: pick.i };
  }

  // No live window on this server. A server the user already chose by hand
  // needs no consent; a brand-new one does (pinning is a privilege grant).
  if (Array.isArray(knownOrigins) && knownOrigins.includes(targetOrigin)) {
    return { strategy: "open-known" };
  }
  return { strategy: "consent-unknown" };
}

module.exports = { parseOmnigentDeepLink, chooseDeepLinkStrategy, DEEP_LINK_PATH_RE };
