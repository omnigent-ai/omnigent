// Shell-owned About window.
//
// The connected app UI is supplied by the server, so product/version details
// live in this bundled Electron page instead. Its narrow preload can only read
// local version information and trigger fixed-purpose update checks.

"use strict";

const { pathToFileURL } = require("node:url");

const ABOUT_WIDTH = 760;
const ABOUT_HEIGHT = 480;

function platformDisplayName(platform) {
  if (platform === "darwin") return "macOS";
  if (platform === "win32") return "Windows";
  if (platform === "linux") return "Linux";
  return "Desktop";
}

/** Resolve the current platform app icon, with the bundled PNG as fallback. */
async function resolveAppIconDataUrl({
  app,
  nativeImage,
  platform = process.platform,
  executablePath = process.execPath,
  fallbackIconPath,
}) {
  const candidates = [];
  if (app.isPackaged && platform === "darwin") {
    candidates.push(() => nativeImage.createFromNamedImage("NSApplicationIcon"));
    candidates.push(() => app.getFileIcon(executablePath, { size: "large" }));
  } else if (app.isPackaged && platform === "win32") {
    candidates.push(() => app.getFileIcon(executablePath, { size: "large" }));
  }
  candidates.push(() => nativeImage.createFromPath(fallbackIconPath));

  async function resolveCandidate(index) {
    if (index >= candidates.length) return null;
    try {
      const image = await candidates[index]();
      if (image && !image.isEmpty()) return image.toDataURL({ scaleFactor: 2 });
    } catch {
      // Try the next source. Native icon lookup can fail on older platforms.
    }
    return resolveCandidate(index + 1);
  }

  return resolveCandidate(0);
}

/**
 * @param {object} deps
 * @param {typeof import("electron").BrowserWindow} deps.BrowserWindow
 * @param {import("electron").IpcMain} deps.ipcMain
 * @param {import("electron").NativeTheme} deps.nativeTheme
 * @param {{ getStatus: Function, checkForUpdates: Function,
 *   downloadUpdate: Function, installUpdateNow: Function }} deps.updater
 * @param {() => string} deps.getDesktopVersion
 * @param {() => Promise<string | null>} deps.getAppIconDataUrl
 * @param {() => Promise<object>} deps.getCliStatus
 * @param {(parent: Electron.BrowserWindow) => void} deps.onDesktopDownloadStarted
 * @param {(parent: Electron.BrowserWindow) => void} deps.onClosed
 * @param {string} deps.aboutPage Absolute path to the bundled About HTML.
 * @param {string} deps.preloadPath Absolute path to about_preload.js.
 * @param {NodeJS.Platform} [deps.platform]
 */
function createAboutWindow({
  BrowserWindow,
  ipcMain,
  nativeTheme,
  updater,
  getDesktopVersion,
  getAppIconDataUrl,
  getCliStatus,
  onDesktopDownloadStarted = () => {},
  onClosed = () => {},
  aboutPage,
  preloadPath,
  platform = process.platform,
}) {
  const aboutPageUrl = pathToFileURL(aboutPage);
  /** @type {Electron.BrowserWindow | null} */
  let about = null;
  /** @type {Electron.BrowserWindow | null} */
  let parent = null;

  function senderIsAbout(event) {
    if (!about || about.isDestroyed() || event.sender !== about.webContents) return false;
    const frameUrl = event.senderFrame?.url ?? event.sender?.getURL?.() ?? "";
    try {
      const url = new URL(frameUrl);
      return url.protocol === "file:" && url.pathname === aboutPageUrl.pathname;
    } catch {
      return false;
    }
  }

  function guard(event) {
    if (!senderIsAbout(event)) {
      throw new Error("About IPC is only available to the bundled About window");
    }
  }

  function closeAbout() {
    if (about && !about.isDestroyed()) about.close();
  }

  function open(nextParent = null) {
    const validParent = nextParent && !nextParent.isDestroyed() ? nextParent : null;
    if (about && !about.isDestroyed()) {
      if (parent === validParent) {
        if (about.isMinimized()) about.restore();
        about.show();
        about.focus();
        return about;
      }
      about.destroy();
    }

    parent = validParent;
    about = new BrowserWindow({
      ...(parent ? { parent } : {}),
      modal: Boolean(parent),
      title: "About Omnigent",
      width: ABOUT_WIDTH,
      height: ABOUT_HEIGHT,
      minWidth: 640,
      minHeight: 420,
      resizable: true,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      show: false,
      backgroundColor: nativeTheme.shouldUseDarkColors ? "#151515" : "#ffffff",
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    if (platform === "darwin") about.excludedFromShownWindowsMenu = true;

    about.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    about.webContents.on("will-navigate", (event, targetUrl) => {
      if (targetUrl !== aboutPageUrl.href) event.preventDefault();
    });

    const opened = about;
    const openedParent = parent;
    const onParentClosed = () => {
      if (!opened.isDestroyed()) opened.destroy();
    };
    openedParent?.on("closed", onParentClosed);
    opened.once("ready-to-show", () => {
      if (!opened.isDestroyed()) opened.show();
    });
    opened.on("closed", () => {
      if (openedParent && !openedParent.isDestroyed()) {
        openedParent.removeListener("closed", onParentClosed);
      }
      if (openedParent) onClosed(openedParent);
      if (about === opened) {
        about = null;
        parent = null;
      }
    });
    void opened.loadFile(aboutPage);
    return opened;
  }

  function registerIpc() {
    ipcMain.handle("omnigent:about-get-info", async (event) => {
      guard(event);
      const [appIconDataUrl, cli] = await Promise.all([getAppIconDataUrl(), getCliStatus()]);
      return {
        desktopVersion: getDesktopVersion(),
        platformName: platformDisplayName(platform),
        appIconDataUrl,
        cli,
      };
    });

    ipcMain.handle("omnigent:about-get-desktop-update-status", (event) => {
      guard(event);
      return updater.getStatus();
    });

    ipcMain.handle("omnigent:about-check-desktop", async (event) => {
      guard(event);
      await updater.checkForUpdates({ manual: true });
      return updater.getStatus();
    });

    ipcMain.handle("omnigent:about-download-desktop-update", async (event) => {
      guard(event);
      if (parent && !parent.isDestroyed()) onDesktopDownloadStarted(parent);
      await updater.downloadUpdate();
    });

    ipcMain.handle("omnigent:about-install-desktop-update", (event) => {
      guard(event);
      if (!updater.installUpdateNow()) {
        throw new Error("No downloaded update is ready to install.");
      }
    });

    ipcMain.on("omnigent:about-close", (event) => {
      if (senderIsAbout(event)) closeAbout();
    });
  }

  return { open, close: closeAbout, registerIpc, senderIsAbout };
}

module.exports = {
  createAboutWindow,
  resolveAppIconDataUrl,
  platformDisplayName,
  ABOUT_WIDTH,
  ABOUT_HEIGHT,
};
