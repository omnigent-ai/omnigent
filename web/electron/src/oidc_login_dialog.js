"use strict";

const { pathToFileURL } = require("node:url");

const OIDC_LOGIN_ACTION_CHANNEL = "omnigent:oidc-login-action";
const OIDC_LOGIN_STATE_CHANNEL = "omnigent:oidc-login-state";

/**
 * Show the shell-owned sign-in modal and keep it open through errors so the
 * user always has Retry and Cancel. The modal has its own preload; no auth
 * controls are exposed to the connected server or IdP pages.
 *
 * @param {{
 *   BrowserWindow: typeof Electron.BrowserWindow,
 *   ipcMain: Electron.IpcMain,
 *   parent: Electron.BrowserWindow,
 *   serverUrl: string,
 *   pagePath: string,
 *   preloadPath: string,
 *   runAttempt: (params: {
 *     signal: AbortSignal,
 *     updateMessage: (message: string) => void,
 *   }) => Promise<
 *     { ok: true } | { ok: false, error: string }
 *   >,
 * }} params
 * @returns {Promise<boolean>} true after a verified login; false on cancel/close.
 */
function runOidcLoginDialog({
  BrowserWindow,
  ipcMain,
  parent,
  serverUrl,
  pagePath,
  preloadPath,
  runAttempt,
}) {
  return new Promise((resolve) => {
    const loginWindow = new BrowserWindow({
      parent,
      modal: true,
      show: false,
      width: 460,
      height: 360,
      minWidth: 420,
      minHeight: 320,
      resizable: false,
      maximizable: false,
      minimizable: false,
      title: "Sign in to Omnigent",
      backgroundColor: "#1e1927",
      webPreferences: {
        preload: preloadPath,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    let host = "the configured server";
    try {
      host = new URL(serverUrl).host;
    } catch {
      // The attempt reports the actionable validation error.
    }
    const allowedPageUrl = pathToFileURL(pagePath).toString();
    let lastState = {
      phase: "waiting",
      host,
      message: "Complete sign-in in the browser window that just opened.",
    };
    let attemptNumber = 0;
    let inFlight = false;
    let controller = null;
    let settled = false;

    const sendState = () => {
      if (!loginWindow.isDestroyed()) {
        loginWindow.webContents.send(OIDC_LOGIN_STATE_CHANNEL, lastState);
      }
    };

    const cleanup = () => {
      ipcMain.removeListener(OIDC_LOGIN_ACTION_CHANNEL, onAction);
      controller?.abort();
    };

    const finish = (ok) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (!loginWindow.isDestroyed()) loginWindow.close();
      resolve(ok);
    };

    const attempt = async () => {
      if (settled || inFlight) return;
      inFlight = true;
      const currentAttempt = ++attemptNumber;
      controller = new AbortController();
      lastState = {
        phase: "waiting",
        host,
        message: "Complete sign-in in the browser window that just opened.",
      };
      sendState();
      const updateMessage = (message) => {
        if (
          settled ||
          currentAttempt !== attemptNumber ||
          typeof message !== "string" ||
          message === ""
        ) {
          return;
        }
        lastState = { phase: "waiting", host, message };
        sendState();
      };
      let result;
      try {
        result = await runAttempt({ signal: controller.signal, updateMessage });
      } catch {
        result = { ok: false, error: "Sign-in failed unexpectedly. Please try again." };
      }
      if (settled || currentAttempt !== attemptNumber) return;
      inFlight = false;
      controller = null;
      if (result.ok) {
        finish(true);
        return;
      }
      lastState = { phase: "error", host, message: result.error };
      sendState();
    };

    function onAction(event, action) {
      if (event.sender !== loginWindow.webContents) return;
      if (action === "cancel") {
        finish(false);
      } else if (action === "retry" && lastState.phase === "error") {
        void attempt();
      }
    }

    ipcMain.on(OIDC_LOGIN_ACTION_CHANNEL, onAction);
    loginWindow.webContents.on("will-navigate", (event, url) => {
      if (url !== allowedPageUrl) event.preventDefault();
    });
    loginWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    loginWindow.webContents.on("did-finish-load", () => {
      sendState();
      if (attemptNumber === 0) void attempt();
    });
    loginWindow.once("ready-to-show", () => loginWindow.show());
    loginWindow.on("closed", () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(false);
    });
    void loginWindow.loadFile(pagePath).catch(() => finish(false));
  });
}

module.exports = {
  OIDC_LOGIN_ACTION_CHANNEL,
  OIDC_LOGIN_STATE_CHANNEL,
  runOidcLoginDialog,
};
