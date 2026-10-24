// One lazily created, non-focusing child banner per shell window. The parent may show
// an untrusted page, so banners load a bundled page with a narrow preload; match IPC by sender.

"use strict";

/**
 * @param {object} deps
 * @param {typeof import("electron").BrowserWindow} deps.BrowserWindow
 * @param {string} deps.page Absolute path to the bundled banner HTML.
 * @param {string} deps.preloadPath Absolute path to the banner's preload.
 * @param {number} deps.width Window width, including the page's shadow gutter.
 * @param {number} deps.height Window height, including the page's shadow gutter.
 * @param {number} deps.top Offset from the top of the parent's content area.
 * @param {(parent: import("electron").BrowserWindow) => void} [deps.onClosed]
 *   Called when a parent's banner closes, to drop per-parent state.
 * @param {NodeJS.Platform} [deps.platform] Runtime platform (injectable for tests).
 */
function createBannerWindows({
  BrowserWindow,
  page,
  preloadPath,
  width,
  height,
  top,
  onClosed,
  platform = process.platform,
}) {
  /** @type {Map<Electron.BrowserWindow, Electron.BrowserWindow>} parent -> banner */
  const banners = new Map();

  /** The parent whose banner sent this IPC event, else null. */
  function parentForSender(event) {
    for (const [parent, banner] of banners) {
      if (!banner.isDestroyed() && banner.webContents === event.sender) return parent;
    }
    return null;
  }

  function position(parent, banner) {
    if (!parent || parent.isDestroyed() || banner.isDestroyed()) return;
    const content = parent.getContentBounds();
    banner.setBounds({
      x: content.x + Math.round((content.width - width) / 2),
      y: content.y + top,
      width,
      height,
    });
  }

  /** Create (once) the banner window for a shell window and load the page. */
  function ensure(parent) {
    const existing = banners.get(parent);
    if (existing && !existing.isDestroyed()) return existing;

    const banner = new BrowserWindow({
      parent,
      frame: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      transparent: true,
      hasShadow: false, // the banner page draws its own border/shadow
      show: false,
      width,
      height,
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    banner.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    banner.webContents.on("will-navigate", (event) => event.preventDefault());
    if (platform === "darwin") banner.excludedFromShownWindowsMenu = true;
    banners.set(parent, banner);
    void banner.loadFile(page);

    const reposition = () => position(parent, banner);
    parent.on("resize", reposition);
    parent.on("move", reposition);
    // Electron does NOT auto-close child windows when their parent closes.
    const onParentClosed = () => {
      if (!banner.isDestroyed()) banner.destroy();
    };
    parent.on("closed", onParentClosed);
    banner.on("closed", () => {
      // A replacement may already be registered if this banner was destroyed before `closed`.
      if (banners.get(parent) === banner) {
        banners.delete(parent);
        onClosed?.(parent);
      }
      if (!parent.isDestroyed()) {
        parent.removeListener("resize", reposition);
        parent.removeListener("move", reposition);
        parent.removeListener("closed", onParentClosed);
      }
    });
    return banner;
  }

  /** Show the parent's banner, creating it on first use. */
  function show(parent) {
    if (!parent || parent.isDestroyed()) return;
    const banner = ensure(parent);
    position(parent, banner);
    // showInactive: never steal focus from the page (e.g. an SSO form).
    if (!banner.isDestroyed() && !banner.isVisible()) banner.showInactive();
  }

  /** Hide the parent's banner (idempotent); the next ``show`` re-displays it. */
  function hide(parent) {
    const banner = banners.get(parent);
    if (banner && !banner.isDestroyed() && banner.isVisible()) banner.hide();
  }

  return { ensure, show, hide, parentForSender };
}

module.exports = { createBannerWindows };
