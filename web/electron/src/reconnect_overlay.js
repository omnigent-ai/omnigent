// Shell-owned "Reconnecting to Databricks…" overlay.
//
// While a Databricks workspace is briefly unreachable (VPN reconnecting after
// wake), the window keeps its page and this overlay dims it behind a spinner
// card with a Cancel button. It is a transparent WebContentsView on top of the
// window's content: it moves with the window, covers browser panes and blocks
// input to the page underneath. The page is bundled and its preload is narrow;
// IPC is verified by sender, since the page underneath may be untrusted.

"use strict";

const { pathToFileURL } = require("node:url");

/**
 * @param {object} deps
 * @param {typeof import("electron").WebContentsView} deps.WebContentsView
 * @param {import("electron").IpcMain} deps.ipcMain
 * @param {string} deps.overlayPage Absolute path to the bundled overlay HTML.
 * @param {string} deps.preloadPath Absolute path to reconnect_overlay_preload.js.
 * @param {(parent: import("electron").BrowserWindow) => void} deps.onCancel
 */
function createReconnectOverlay({ WebContentsView, ipcMain, overlayPage, preloadPath, onCancel }) {
  const pageUrl = pathToFileURL(overlayPage).href;
  /** @type {Map<Electron.BrowserWindow, {view: Electron.WebContentsView, hint: string | null, shown: boolean, fit: () => void}>} */
  const overlays = new Map();

  function parentForSender(event) {
    for (const [parent, overlay] of overlays) {
      const wc = overlay.view.webContents;
      if (event.sender === wc && event.senderFrame?.url?.split("?")[0] === pageUrl) return parent;
    }
    return null;
  }

  function ensureOverlay(parent) {
    const existing = overlays.get(parent);
    if (existing) return existing;
    const view = new WebContentsView({
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        devTools: false,
      },
    });
    view.setBackgroundColor("#00000000");
    view.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    view.webContents.on("will-navigate", (event) => event.preventDefault());
    const overlay = { view, hint: null, shown: false, fit: () => {} };
    overlay.fit = () => {
      if (parent.isDestroyed()) return;
      const [width, height] = parent.getContentSize();
      view.setBounds({ x: 0, y: 0, width, height });
    };
    parent.on("resize", overlay.fit);
    parent.once("closed", () => {
      overlays.delete(parent);
      if (!view.webContents.isDestroyed()) view.webContents.close();
    });
    overlays.set(parent, overlay);
    return overlay;
  }

  /** Show (or update) the overlay over a window's current page. */
  function show(parent, hint) {
    if (!parent || parent.isDestroyed()) return;
    const overlay = ensureOverlay(parent);
    if (overlay.hint !== hint) {
      overlay.hint = hint;
      void overlay.view.webContents.loadFile(overlayPage, { query: { hint } }).catch(() => {});
    }
    overlay.fit();
    // Re-adding moves the view to the top, above any browser pane.
    parent.contentView.addChildView(overlay.view);
    overlay.shown = true;
    overlay.view.webContents.focus();
  }

  /** Hide the overlay (idempotent); the view is kept for the next show. */
  function hide(parent) {
    const overlay = overlays.get(parent);
    if (!overlay?.shown || parent.isDestroyed()) return;
    overlay.shown = false;
    parent.contentView.removeChildView(overlay.view);
    // Hand keyboard focus back to the page the overlay covered.
    if (parent.isFocused?.()) parent.webContents.focus();
  }

  function isShown(parent) {
    return Boolean(overlays.get(parent)?.shown);
  }

  /** Keep the overlay above a view attached while it is shown. */
  function raise(parent) {
    const overlay = overlays.get(parent);
    if (overlay?.shown && !parent.isDestroyed()) parent.contentView.addChildView(overlay.view);
  }

  function registerIpc() {
    ipcMain.on("omnigent:reconnect-overlay-cancel", (event) => {
      const parent = parentForSender(event);
      if (!parent || parent.isDestroyed() || !isShown(parent)) return;
      onCancel(parent);
    });
  }

  return { show, hide, isShown, raise, registerIpc };
}

module.exports = { createReconnectOverlay };
