"use strict";

const { FLOATING_COMPOSER_PATH } = require("./floating_composer_window");

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
 * Which page of the server at `serverUrl` a URL shows: its floating `"panel"`, its `"sign-in"`
 * page (accounts `/login` or `/register`, or an OIDC `/auth/…` route), the rest of its `"app"`,
 * or null for anything not under the server's mount: another server on the same origin,
 * another site, or the bundled setup page. The one place main decides this.
 *
 * @param {string} url
 * @param {string | null | undefined} serverUrl
 * @returns {"panel" | "app" | "sign-in" | null}
 */
function serverPageOf(url, serverUrl) {
  if (!serverUrl) return null;
  let page;
  let server;
  try {
    page = new URL(url);
    server = new URL(serverUrl);
  } catch {
    return null;
  }
  if (page.origin !== server.origin) return null;
  // The mount as resolveServerPath joins routes onto it: the server's path, no trailing slash.
  const mount = server.pathname.replace(/\/+$/, "");
  if (mount !== "" && page.pathname !== mount && !page.pathname.startsWith(`${mount}/`)) {
    return null;
  }
  const route = page.pathname.slice(mount.length) || "/";
  if (route === FLOATING_COMPOSER_PATH) return "panel";
  if (route === "/login" || route === "/register" || route.startsWith("/auth/")) return "sign-in";
  return "app";
}

module.exports = {
  createFloatingComposerSupport,
  floatingComposerTarget,
  mainWindowsByRecency,
  serverPageOf,
};
