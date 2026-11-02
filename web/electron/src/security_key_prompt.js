// Shell-owned security-key prompt shown while a shell page's WebAuthn request is pending.

"use strict";

const { createBannerWindows } = require("./banner_window");
const { BANNER_HEIGHT, BANNER_INSET } = require("./return_banner");

const PROMPT_WIDTH = 480;
const PROMPT_HEIGHT = 152;

/**
 * @param {object} deps
 * @param {typeof import("electron").BrowserWindow} deps.BrowserWindow
 * @param {import("electron").IpcMain} deps.ipcMain
 * @param {string} deps.promptPage Absolute path to the bundled prompt HTML.
 * @param {string} deps.preloadPath Absolute path to security_key_prompt_preload.js.
 * @param {(win: import("electron").BrowserWindow) => boolean} deps.isShellWindow
 *   Whether a window is one of the shell's server windows.
 * @param {NodeJS.Platform} [deps.platform] Runtime platform (injectable for tests).
 */
function createSecurityKeyPrompt({
  BrowserWindow,
  ipcMain,
  promptPage,
  preloadPath,
  isShellWindow,
  platform,
}) {
  const prompts = createBannerWindows({
    BrowserWindow,
    page: promptPage,
    preloadPath,
    width: PROMPT_WIDTH,
    height: PROMPT_HEIGHT,
    top: BANNER_INSET + BANNER_HEIGHT,
    platform,
  });

  /** Hide the window's prompt whenever its page goes away. */
  function attach(win) {
    const hide = () => prompts.hide(win);
    win.webContents.on("did-navigate", hide);
    win.webContents.on("render-process-gone", hide);
  }

  function registerIpc() {
    ipcMain.on("omnigent:webauthn-pending", (event, pending) => {
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!win || win.webContents !== event.sender || !isShellWindow(win)) return;
      if (event.senderFrame !== event.sender.mainFrame) return;
      if (pending === true) prompts.show(win);
      else prompts.hide(win);
    });
    ipcMain.on("omnigent:security-key-prompt-dismiss", (event) => {
      const parent = prompts.parentForSender(event);
      if (parent) prompts.hide(parent);
    });
  }

  return { attach, registerIpc };
}

module.exports = { createSecurityKeyPrompt, PROMPT_WIDTH, PROMPT_HEIGHT };
