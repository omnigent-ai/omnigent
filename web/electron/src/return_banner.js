// Shell-owned "return to your server?" banner, shown while a shell window sits on a
// foreign (typically SSO) page; see away_banner.js. That page is untrusted, so the banner
// is a separate child window (banner_window.js) whose IPC is verified by sender.

"use strict";

const { createBannerWindows } = require("./banner_window");

// The window is larger than the card by the gutter on every side: a
// transparent window clips painting at its square bounds, so the card needs
// interior room for its border radius and shadow (the update overlay's
// "shadow gutter" pattern). Keep the gutter small — it is invisible dead
// space between the card and the window edge.
const BANNER_WIDTH = 440;
const BANNER_HEIGHT = 64;
const BANNER_INSET = 12;

/**
 * @param {object} deps
 * @param {typeof import("electron").BrowserWindow} deps.BrowserWindow
 * @param {import("electron").IpcMain} deps.ipcMain
 * @param {string} deps.bannerPage Absolute path to the bundled banner HTML.
 * @param {string} deps.preloadPath Absolute path to return_banner_preload.js.
 * @param {(parent: import("electron").BrowserWindow) => void} [deps.onGoBack]
 *   Called when the user clicks "Go back", before the parent reloads — lets
 *   the away-watch re-arm for the case where the return lands on SSO again.
 * @param {NodeJS.Platform} [deps.platform] Runtime platform (injectable for tests).
 */
function createReturnBanner({
  BrowserWindow,
  ipcMain,
  bannerPage,
  preloadPath,
  onGoBack,
  platform,
}) {
  /** @type {Map<Electron.BrowserWindow, string>} parent -> URL "Go back" loads */
  const returnUrls = new Map();
  const banners = createBannerWindows({
    BrowserWindow,
    page: bannerPage,
    preloadPath,
    width: BANNER_WIDTH,
    height: BANNER_HEIGHT,
    top: BANNER_INSET,
    onClosed: (parent) => returnUrls.delete(parent),
    platform,
  });

  /**
   * Show the banner on a shell window, offering to return to ``returnUrl``.
   *
   * @param {Electron.BrowserWindow} parent
   * @param {string | null} returnUrl Full URL to load on "Go back"
   *   (subpage, mount path, and query args preserved). No-op when null.
   */
  function show(parent, returnUrl) {
    if (!parent || parent.isDestroyed()) return;
    if (!returnUrl) {
      console.warn("[omnigent] return-banner: NOT shown — no return URL recorded");
      return;
    }
    returnUrls.set(parent, returnUrl);
    banners.show(parent);
    console.warn(`[omnigent] return-banner: shown (return to ${returnUrl})`);
  }

  function registerIpc() {
    ipcMain.on("omnigent:return-banner-go-back", (event) => {
      const parent = banners.parentForSender(event);
      const target = parent && returnUrls.get(parent);
      if (!parent || parent.isDestroyed() || !target) return;
      banners.hide(parent);
      // Re-arm before navigating: the return load can land on SSO again
      // (expired session) and that away episode must notify afresh.
      onGoBack?.(parent);
      void parent.loadURL(target).catch(() => {});
    });
    ipcMain.on("omnigent:return-banner-dismiss", (event) => {
      const parent = banners.parentForSender(event);
      if (parent) banners.hide(parent);
    });
  }

  return { ensureBanner: banners.ensure, show, hide: banners.hide, registerIpc };
}

module.exports = { createReturnBanner, BANNER_WIDTH, BANNER_HEIGHT, BANNER_INSET };
