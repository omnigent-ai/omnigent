"use strict";

const { pathToFileURL } = require("node:url");

const PROMPT_WIDTH = 380;
const PROMPT_HEIGHT = 250;
const CHOICES = new Set(["allow-once", "always-allow", "deny", "dismiss"]);

function promptBounds(content, anchor) {
  const width = Math.min(PROMPT_WIDTH, content.width);
  const height = Math.min(PROMPT_HEIGHT, content.height);
  return {
    x: Math.round(content.x + Math.max(0, Math.min(anchor.x + 88, content.width - width))),
    y: Math.round(content.y + Math.max(0, Math.min(anchor.y - 4, content.height - height))),
    width,
    height,
  };
}

/** A trusted child window keeps consent outside both the site and server SPA. */
function createBrowserPermissionPrompt({ BrowserWindow, ipcMain, promptPage, preloadPath }) {
  const pageUrl = pathToFileURL(promptPage).href;
  const prompts = new Map();

  function forSender(event) {
    for (const request of prompts.values()) {
      const wc = request.window.webContents;
      if (
        !request.window.isDestroyed() &&
        event.sender === wc &&
        event.senderFrame === wc.mainFrame &&
        event.senderFrame?.url === pageUrl
      )
        return request;
    }
    throw new Error("Permission choices require the bundled permission UI");
  }

  function dismiss(parent) {
    prompts.get(parent)?.finish("dismiss");
  }

  function show({ parent, origin, reload, getAnchorBounds, signal }) {
    if (!parent || parent.isDestroyed() || signal.aborted) return Promise.resolve("dismiss");
    dismiss(parent);
    return new Promise((resolve) => {
      const popup = new BrowserWindow({
        parent,
        frame: false,
        transparent: true,
        hasShadow: false,
        resizable: false,
        movable: false,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        hiddenInMissionControl: true,
        show: false,
        width: PROMPT_WIDTH,
        height: PROMPT_HEIGHT,
        webPreferences: {
          preload: preloadPath,
          partition: "omnigent-browser-permission-ui",
          nodeIntegration: false,
          contextIsolation: true,
          sandbox: true,
          devTools: false,
        },
      });
      if (process.platform === "darwin") popup.excludedFromShownWindowsMenu = true;
      let finished = false;
      const close = () => finish("dismiss");
      const position = () => {
        if (!parent.isDestroyed() && !popup.isDestroyed()) {
          popup.setBounds(promptBounds(parent.getContentBounds(), getAnchorBounds()));
        }
      };
      const onNavigation = (_event, _url, isInPlace, isMainFrame) => {
        if (isMainFrame && !isInPlace) close();
      };
      function finish(choice) {
        if (finished) return;
        finished = true;
        if (prompts.get(parent)?.window === popup) prompts.delete(parent);
        signal.removeEventListener("abort", close);
        for (const event of ["closed", "hide", "minimize"]) parent.removeListener(event, close);
        for (const event of ["move", "resize"]) parent.removeListener(event, position);
        if (!parent.webContents.isDestroyed()) {
          parent.webContents.removeListener("did-start-navigation", onNavigation);
        }
        if (!popup.isDestroyed()) popup.destroy();
        resolve(choice);
      }
      prompts.set(parent, { window: popup, origin, reload, finish });
      signal.addEventListener("abort", close, { once: true });
      for (const event of ["closed", "hide", "minimize"]) parent.on(event, close);
      for (const event of ["move", "resize"]) parent.on(event, position);
      parent.webContents.on("did-start-navigation", onNavigation);
      popup.on("closed", close);
      popup.on("blur", close);
      popup.webContents.on("render-process-gone", close);
      popup.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      popup.webContents.on("will-navigate", (event) => event.preventDefault());
      popup.webContents.on("will-frame-navigate", (event) => event.preventDefault());
      popup.webContents.session.setPermissionRequestHandler((_wc, _permission, cb) => cb(false));
      popup.webContents.session.setPermissionCheckHandler(() => false);
      popup.once("ready-to-show", () => {
        if (finished) return;
        position();
        popup.show();
      });
      void popup.loadFile(promptPage).catch(close);
    });
  }

  function registerIpc() {
    ipcMain.handle("omnigent:browser-permission-info", (event) => {
      const { origin, reload } = forSender(event);
      return { origin, reload };
    });
    ipcMain.handle("omnigent:browser-permission-choose", (event, choice) => {
      const request = forSender(event);
      if (!CHOICES.has(choice)) throw new Error("Invalid permission choice");
      request.finish(choice);
    });
  }

  return { show, dismiss, registerIpc };
}

module.exports = { createBrowserPermissionPrompt, promptBounds, PROMPT_WIDTH, PROMPT_HEIGHT };
