"use strict";

/**
 * Which main-window documents can render the floating New Agent panel, and which server
 * ⇧⌘Space opens it for.
 *
 * Support belongs to one loaded document of one server: a window that navigates to another
 * document, or another server, loses it until the new document announces again. So an older
 * web bundle never inherits support from a newer one on the same origin.
 */

function createFloatingComposerSupport() {
  /** @type {Map<number, string>} webContents id → the server its current document announced for */
  const documents = new Map();
  return {
    /**
     * @param {number} documentOwnerId
     * @param {string} serverUrl
     * @returns {boolean} whether anything changed
     */
    announce(documentOwnerId, serverUrl) {
      if (documents.get(documentOwnerId) === serverUrl) return false;
      documents.set(documentOwnerId, serverUrl);
      return true;
    },
    /** The document was replaced or closed. @returns {boolean} whether anything changed */
    revoke(documentOwnerId) {
      return documents.delete(documentOwnerId);
    },
    /** @returns {boolean} */
    supports(documentOwnerId, serverUrl) {
      return typeof serverUrl === "string" && documents.get(documentOwnerId) === serverUrl;
    },
    /** Whether any document can render the panel now. */
    any() {
      return documents.size > 0;
    },
  };
}

/**
 * The server the panel opens for. A focused panel keeps its own server, so the shortcut
 * refocuses it; otherwise the most recently focused main window decides, and only if its
 * current document announced support.
 *
 * @template W
 * @param {{
 *   focused: W | null,
 *   panelServerUrl: (win: W) => string | null,
 *   mainWindowsByRecency: W[],
 *   serverUrlOf: (win: W) => string | null,
 *   supported: (win: W, serverUrl: string) => boolean,
 * }} facts
 * @returns {string | null}
 */
function floatingComposerTarget(facts) {
  if (facts.focused !== null) {
    const panelServer = facts.panelServerUrl(facts.focused);
    if (panelServer !== null) return panelServer;
  }
  const recent = facts.mainWindowsByRecency[0];
  if (recent === undefined) return null;
  const serverUrl = facts.serverUrlOf(recent);
  if (serverUrl === null) return null;
  return facts.supported(recent, serverUrl) ? serverUrl : null;
}

/**
 * Main windows, most recently focused first; a window that never took focus comes after
 * those that did, in creation order.
 *
 * @template W
 * @param {W[]} focusOrder
 * @param {W[]} allMainWindows
 * @returns {W[]}
 */
function mainWindowsByRecency(focusOrder, allMainWindows) {
  const live = new Set(allMainWindows);
  const ordered = focusOrder.filter((win) => live.has(win));
  return [...ordered, ...allMainWindows.filter((win) => !ordered.includes(win))];
}

/**
 * Whether `url` is a page of the server at `serverUrl`: the same origin, and at or under its
 * mount path, so another server mounted on the same origin doesn't count.
 *
 * @param {string} url
 * @param {string} serverUrl
 * @returns {boolean}
 */
function isPageOfServer(url, serverUrl) {
  try {
    const page = new URL(url);
    const server = new URL(serverUrl);
    if (page.origin !== server.origin) return false;
    const mount = server.pathname.replace(/\/+$/, "");
    return mount === "" || page.pathname === mount || page.pathname.startsWith(`${mount}/`);
  } catch {
    return false;
  }
}

/**
 * Whether `url` is the sign-in page of the server mounted at `serverUrl`: accounts `/login`
 * or `/register`, or an OIDC `/auth/…` route, under the mount.
 *
 * @param {string} url
 * @param {string | null} serverUrl
 */
function isServerSignInPage(url, serverUrl) {
  if (!serverUrl || !isPageOfServer(url, serverUrl)) return false;
  try {
    const mount = new URL(serverUrl).pathname.replace(/\/+$/, "");
    const route = new URL(url).pathname.slice(mount.length) || "/";
    return route === "/login" || route === "/register" || route.startsWith("/auth/");
  } catch {
    return false;
  }
}

module.exports = {
  createFloatingComposerSupport,
  floatingComposerTarget,
  isPageOfServer,
  isServerSignInPage,
  mainWindowsByRecency,
};
