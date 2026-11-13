"use strict";

const { pathToFileURL } = require("node:url");

const PROMPT_WIDTH = 380;
const PROMPT_HEIGHT = 250;
const CHOICES = {
  permission: new Set(["allow-once", "always-allow", "deny", "dismiss"]),
  "security-key": new Set(["open-in-browser", "dismiss"]),
};
// A security-key notice the app closes is "suppressed" so it can return; only Dismiss or
// Escape is final.
const appClosedChoice = (kind) => (kind === "security-key" ? "suppressed" : "dismiss");

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

/** A trusted child window keeps pane prompts outside both the site and server SPA. */
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
    const current = prompts.get(parent);
    current?.finish(appClosedChoice(current.kind));
  }

  function show({
    parent,
    getAnchorBounds,
    signal,
    kind = "permission",
    origin,
    reload,
    cookieImport,
  }) {
    if (!parent || parent.isDestroyed() || signal.aborted) return Promise.resolve("dismiss");
    const current = prompts.get(parent);
    // The advisory security-key notice yields to a permission decision; "suppressed" asks it
    // to retry once that decision is made.
    if (kind === "security-key" && current?.kind === "permission") {
      return new Promise((resolve) => {
        const abort = () => resolve("dismiss");
        signal.addEventListener("abort", abort, { once: true });
        void current.result.then(() => {
          signal.removeEventListener("abort", abort);
          resolve("suppressed");
        });
      });
    }
    current?.finish(appClosedChoice(current.kind));
    const result = new Promise((resolve) => {
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
      const hidden = () => finish(appClosedChoice(kind));
      const position = () => {
        if (!parent.isDestroyed() && !popup.isDestroyed()) {
          popup.setBounds(promptBounds(parent.getContentBounds(), getAnchorBounds()));
        }
      };
      const onNavigation = (_event, _url, isInPlace, isMainFrame) => {
        if (isMainFrame && !isInPlace) hidden();
      };
      function finish(choice) {
        if (finished) return;
        finished = true;
        if (prompts.get(parent)?.window === popup) prompts.delete(parent);
        signal.removeEventListener("abort", close);
        parent.removeListener("closed", close);
        for (const event of ["hide", "minimize"]) parent.removeListener(event, hidden);
        for (const event of ["move", "resize"]) parent.removeListener(event, position);
        if (!parent.webContents.isDestroyed()) {
          parent.webContents.removeListener("did-start-navigation", onNavigation);
        }
        if (!popup.isDestroyed()) popup.destroy();
        resolve(choice);
      }
      const info =
        kind === "security-key"
          ? { kind, origin, cookieImport: Boolean(cookieImport) }
          : { kind, origin, reload: Boolean(reload) };
      prompts.set(parent, { window: popup, kind, info, finish });
      signal.addEventListener("abort", close, { once: true });
      parent.on("closed", close);
      for (const event of ["hide", "minimize"]) parent.on(event, hidden);
      for (const event of ["move", "resize"]) parent.on(event, position);
      parent.webContents.on("did-start-navigation", onNavigation);
      popup.on("closed", close);
      // The security-key notice reports a request still in progress, so focus moving to
      // another app, the page or another conversation leaves it open.
      if (kind === "permission") popup.on("blur", close);
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
    if (prompts.has(parent)) prompts.get(parent).result = result;
    return result;
  }

  function registerIpc() {
    ipcMain.handle("omnigent:browser-permission-info", (event) => forSender(event).info);
    ipcMain.handle("omnigent:browser-permission-choose", (event, choice) => {
      const request = forSender(event);
      if (!CHOICES[request.kind].has(choice)) throw new Error("Invalid permission choice");
      request.finish(choice);
    });
  }

  return { show, dismiss, registerIpc };
}

module.exports = { createBrowserPermissionPrompt, promptBounds, PROMPT_WIDTH, PROMPT_HEIGHT };
