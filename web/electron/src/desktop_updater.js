// Desktop auto-update orchestration for the Omnigent Electron shell.
//
// Everything the shell needs to check for, download, and install its own
// updates via `electron-updater` lives here, behind a small factory API. The
// main process constructs one updater with its main-process dependencies
// injected (`app`, `BrowserWindow`, `ipcMain`, `dialog`, `nativeImage`, the
// `electron-updater` `autoUpdater`, settings load/save, and the trusted-sender
// checks), then wires four thin seams to it: startup `init()`, the "Updates"
// menu, the `omnigent:*update*` IPC surface (`registerIpc()`), and the
// before-quit install handoff (`quitAndInstallIfPending()`).
//
// Design notes preserved from the original inline implementation:
//   - Updates are gated on a usable feed: a packaged build, or a dev build with
//     `OMNIGENT_FORCE_DEV_UPDATE_CONFIG=1` (surfaced here as `forceDevUpdateConfig`).
//     Manual actions in an unusable feed reject with a friendly dev message.
//   - `autoDownload` is always off; downloads and installs are explicit and
//     each privileged IPC action re-confirms with a native dialog (a cached
//     hosting grant must not silently authorize an update action).
//   - Status is broadcast to every window AND cached (`getStatus()`), so a
//     freshly-loaded renderer can replay the latest state without a round trip.
//   - v1 is stable-only and rides electron-updater's default "latest" channel.

"use strict";

/** Update modes persisted in settings.json under `update_mode`. */
const UPDATE_MODES = new Set(["none", "manual", "start", "default"]);
const UPDATES_UNAVAILABLE_IN_DEV = "Desktop updates are unavailable in development builds.";
/** Background re-check cadence for the "default" mode (every 6 hours). */
const PERIODIC_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

/**
 * Normalize the raw settings object into the update config the renderer sees.
 * Unknown/missing values fall back to the safe defaults (auto-checking on,
 * auto-install on, nothing skipped).
 *
 * @param {Record<string, unknown>} settings
 * @returns {{ mode: string, autoInstall: boolean, skippedVersion: string | null }}
 */
function normalizeUpdateConfig(settings) {
  const mode = UPDATE_MODES.has(settings.update_mode) ? settings.update_mode : "default";
  return {
    mode,
    autoInstall:
      typeof settings.update_auto_install === "boolean" ? settings.update_auto_install : true,
    skippedVersion:
      typeof settings.update_skipped_version === "string" ? settings.update_skipped_version : null,
  };
}

/**
 * True when an updater error message looks like a code-signing / integrity
 * failure (as opposed to a transient network/404). Those are surfaced to the
 * user as `error-security` even outside a manual check.
 *
 * @param {string} message
 * @returns {boolean}
 */
function isUpdateSecurityError(message) {
  return /signature|sha512|checksum|not signed|code sign/i.test(message);
}

/**
 * Build the desktop updater. All host dependencies are injected so the module
 * carries no Electron/`main.js` globals and is directly unit-testable with
 * fakes.
 *
 * @param {object} deps
 * @param {import("electron").App} deps.app
 * @param {typeof import("electron").BrowserWindow} deps.BrowserWindow
 * @param {import("electron").IpcMain} deps.ipcMain
 * @param {import("electron").Dialog} deps.dialog
 * @param {import("electron").NativeImage} deps.nativeImage The `nativeImage` module.
 * @param {import("electron-updater").AppUpdater} deps.autoUpdater
 * @param {() => Record<string, unknown>} deps.loadSettings
 * @param {(settings: Record<string, unknown>) => void} deps.saveSettings
 * @param {(event: Electron.IpcMainInvokeEvent) => boolean} deps.isPinnedOriginSender
 *   Whether an IPC call came from a connected server's own pinned page.
 * @param {(win: Electron.BrowserWindow | null | undefined) => string | null} deps.pinnedOrigin
 *   The origin a window is pinned to (used for the consent dialog copy).
 * @param {string} deps.iconPath Absolute path to the app icon PNG.
 * @param {boolean} [deps.forceDevUpdateConfig] Force the dev feed on in an
 *   unpackaged build (from `OMNIGENT_FORCE_DEV_UPDATE_CONFIG=1`).
 * @returns {{
 *   getConfig: () => { mode: string, autoInstall: boolean, skippedVersion: string | null },
 *   setConfig: (patch?: object) => { mode: string, autoInstall: boolean, skippedVersion: string | null },
 *   getStatus: () => object,
 *   init: () => void,
 *   checkForUpdates: (opts?: { manual?: boolean }) => Promise<void>,
 *   installUpdateNow: () => boolean,
 *   registerIpc: () => void,
 *   quitAndInstallIfPending: () => boolean,
 *   readonly installPending: boolean,
 * }}
 */
function createDesktopUpdater({
  app,
  BrowserWindow,
  ipcMain,
  dialog,
  nativeImage,
  autoUpdater,
  loadSettings,
  saveSettings,
  isPinnedOriginSender,
  pinnedOrigin,
  iconPath,
  forceDevUpdateConfig = false,
}) {
  let updateCheckTimer = null;
  let currentUpdateStatus = { state: "idle" };
  let installPending = false;
  // A user-initiated ("Check for Updates…") check in flight — lets the error
  // handler surface a failure to the user only when they explicitly asked.
  let manualCheckInFlight = false;

  function getConfig() {
    return normalizeUpdateConfig(loadSettings());
  }

  function setConfig(patch = {}) {
    const settings = loadSettings();
    const next = { ...settings };
    if (Object.prototype.hasOwnProperty.call(patch, "mode") && UPDATE_MODES.has(patch.mode)) {
      next.update_mode = patch.mode;
    }
    if (
      Object.prototype.hasOwnProperty.call(patch, "autoInstall") &&
      typeof patch.autoInstall === "boolean"
    ) {
      next.update_auto_install = patch.autoInstall;
    }
    if (Object.prototype.hasOwnProperty.call(patch, "skippedVersion")) {
      next.update_skipped_version =
        typeof patch.skippedVersion === "string" ? patch.skippedVersion : null;
    }
    saveSettings(next);
    return getConfig();
  }

  function getStatus() {
    return currentUpdateStatus;
  }

  function broadcast(status) {
    currentUpdateStatus = status;
    for (const win of BrowserWindow.getAllWindows()) {
      if (win.isDestroyed()) continue;
      try {
        win.webContents.send("omnigent:update-status", status);
      } catch {
        // Window torn down between enumeration and send; ignore.
      }
    }
  }

  function canUseFeed() {
    return app.isPackaged || autoUpdater.forceDevUpdateConfig === true;
  }

  function reportUnavailableInDev() {
    broadcast({ state: "idle", lastError: UPDATES_UNAVAILABLE_IN_DEV });
  }

  function unavailableInDevError() {
    return new Error(UPDATES_UNAVAILABLE_IN_DEV);
  }

  function applyConfig(cfg) {
    if (updateCheckTimer) {
      clearInterval(updateCheckTimer);
      updateCheckTimer = null;
    }
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = cfg.autoInstall;
    // v1 is stable-only and rides electron-updater's default "latest" channel.
    if (!canUseFeed() || cfg.mode === "none") return;
    if (cfg.mode === "manual") return;
    autoUpdater.checkForUpdates().catch(() => {});
    if (cfg.mode === "default") {
      updateCheckTimer = setInterval(
        () => autoUpdater.checkForUpdates().catch(() => {}),
        PERIODIC_CHECK_INTERVAL_MS,
      );
    }
  }

  function init() {
    const cfg = getConfig();
    if (forceDevUpdateConfig) {
      autoUpdater.forceDevUpdateConfig = true;
    }
    autoUpdater.on("checking-for-update", () => broadcast({ state: "checking" }));
    autoUpdater.on("update-available", (info) => {
      manualCheckInFlight = false;
      broadcast({ state: "available", info });
    });
    autoUpdater.on("update-not-available", () => {
      manualCheckInFlight = false;
      broadcast({ state: "none" });
    });
    autoUpdater.on("download-progress", (progress) =>
      broadcast({ state: "downloading", progress }),
    );
    autoUpdater.on("update-downloaded", (info) => broadcast({ state: "downloaded", info }));
    autoUpdater.on("error", (err) => {
      const msg = String(err?.message ?? err);
      const isSecurity = isUpdateSecurityError(msg);
      const wasManualCheck = manualCheckInFlight;
      manualCheckInFlight = false;
      console[isSecurity ? "error" : "warn"]("[omnigent] update error:", msg);
      if (isSecurity || wasManualCheck) {
        broadcast({ state: isSecurity ? "error-security" : "idle", lastError: msg });
      }
    });
    applyConfig(cfg);
  }

  function checkForUpdates({ manual = false } = {}) {
    if (!canUseFeed()) {
      if (manual) reportUnavailableInDev();
      return Promise.reject(unavailableInDevError());
    }
    if (manual) manualCheckInFlight = true;
    return autoUpdater
      .checkForUpdates()
      .then(() => {
        if (manual) manualCheckInFlight = false;
        return undefined;
      })
      .catch((err) => {
        const msg = String(err?.message ?? err);
        if (manualCheckInFlight) {
          manualCheckInFlight = false;
          broadcast({
            state: isUpdateSecurityError(msg) ? "error-security" : "idle",
            lastError: msg,
          });
        }
        throw err;
      });
  }

  function installUpdateNow() {
    if (!canUseFeed()) {
      reportUnavailableInDev();
      return false;
    }
    if (currentUpdateStatus.state !== "downloaded") return false;
    installPending = true;
    app.quit();
    return true;
  }

  /**
   * Native, per-action consent for a privileged update control. The IPC gate
   * only proves the call came FROM the pinned server's page — not that the USER
   * asked for it — so download/install/config changes re-confirm here. Returns
   * true only when the user picks "Allow Once".
   */
  async function confirmControl(win, action) {
    if (!win) return false;
    const pinned = pinnedOrigin(win);
    if (!pinned) return false;

    let host = pinned;
    try {
      host = new URL(pinned).host;
    } catch {
      // Keep the full origin string if it somehow doesn't parse.
    }

    const copy = {
      download: {
        message: "Download an Omnigent update?",
        detail:
          `${host} wants to download a desktop update for this Omnigent app.\n\n` +
          `Only allow servers you trust.`,
      },
      install: {
        message: "Restart Omnigent to install an update?",
        detail:
          `${host} wants to restart Omnigent and install the downloaded desktop update.\n\n` +
          `Only allow servers you trust.`,
      },
      config: {
        message: "Change Omnigent update settings?",
        detail:
          `${host} wants to change how this Omnigent app checks for and installs updates.\n\n` +
          `Only allow servers you trust.`,
      },
    }[action];
    if (!copy) return false;

    const icon = nativeImage.createFromPath(iconPath);
    const { response } = await dialog.showMessageBox(win, {
      type: "warning",
      icon: icon.isEmpty() ? undefined : icon,
      title: "Omnigent",
      message: copy.message,
      detail: copy.detail,
      buttons: ["Don't Allow", "Allow Once"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    return response === 1;
  }

  function registerIpc() {
    ipcMain.handle("omnigent:get-update-config", (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("get-update-config is only available to a connected server page");
      }
      return getConfig();
    });

    ipcMain.handle("omnigent:get-update-status", (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("get-update-status is only available to a connected server page");
      }
      return currentUpdateStatus;
    });

    ipcMain.handle("omnigent:update-check", async (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("update-check is only available to a connected server page");
      }
      await checkForUpdates({ manual: true });
    });

    ipcMain.handle("omnigent:update-download", async (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("update-download is only available to a connected server page");
      }
      if (!canUseFeed()) {
        reportUnavailableInDev();
        throw unavailableInDevError();
      }
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!(await confirmControl(win, "download"))) {
        throw new Error("Update download wasn't approved for this server.");
      }
      await autoUpdater.downloadUpdate();
    });

    ipcMain.handle("omnigent:update-install", async (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("update-install is only available to a connected server page");
      }
      if (!canUseFeed()) {
        reportUnavailableInDev();
        throw unavailableInDevError();
      }
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!(await confirmControl(win, "install"))) {
        throw new Error("Update install wasn't approved for this server.");
      }
      if (!installUpdateNow()) {
        throw new Error("No downloaded update is ready to install.");
      }
    });

    ipcMain.handle("omnigent:set-update-config", async (event, patch) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("set-update-config is only available to a connected server page");
      }
      if (!canUseFeed()) {
        reportUnavailableInDev();
        throw unavailableInDevError();
      }
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!(await confirmControl(win, "config"))) {
        throw new Error("Update settings change wasn't approved for this server.");
      }
      const cfg = setConfig(patch);
      applyConfig(cfg);
      return cfg;
    });
  }

  /**
   * Complete a deferred install once the app's own before-quit cleanup has
   * finished. No-op unless a user-approved install is pending.
   *
   * @returns {boolean} Whether a pending install was handed off.
   */
  function quitAndInstallIfPending() {
    if (!installPending) return false;
    autoUpdater.quitAndInstall(false, true);
    return true;
  }

  return {
    getConfig,
    setConfig,
    getStatus,
    init,
    checkForUpdates,
    installUpdateNow,
    registerIpc,
    quitAndInstallIfPending,
    get installPending() {
      return installPending;
    },
  };
}

module.exports = {
  createDesktopUpdater,
  normalizeUpdateConfig,
  isUpdateSecurityError,
  UPDATE_MODES,
  UPDATES_UNAVAILABLE_IN_DEV,
};
