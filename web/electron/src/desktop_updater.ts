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
//     `forceDevUpdateConfig` set (main.js derives it from !app.isPackaged, so
//     dev always uses dev-app-update.yml and a packaged build can never be
//     redirected to it). Manual actions in an unusable feed reject with a
//     friendly dev message.
//   - `autoDownload` is always off; downloads and installs are explicit and
//     each privileged IPC action re-confirms with a native dialog (a cached
//     hosting grant must not silently authorize an update action).
//   - Status is broadcast to every window AND cached (`getStatus()`), so a
//     freshly-loaded renderer can replay the latest state without a round trip.
//   - v1 is stable-only and rides electron-updater's default "latest" channel.

import type { UpdateConfig, UpdateMode } from "./ipc/bridge.ts";

export type { UpdateConfig, UpdateMode };

/** Update modes persisted in settings.json under `update_mode`. */
export const UPDATE_MODES: ReadonlySet<UpdateMode> = new Set<UpdateMode>([
  "none",
  "manual",
  "start",
  "default",
]);
export const UPDATES_UNAVAILABLE_IN_DEV = "Desktop updates are unavailable in development builds.";
/** Background re-check cadence for the "default" mode (every 6 hours). */
const PERIODIC_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

export type UpdateState =
  "idle" | "checking" | "available" | "none" | "downloading" | "downloaded" | "error-security";

/** The latest updater state, broadcast to every window and cached for replay. */
export interface UpdateStatus {
  state: UpdateState;
  lastError?: string;
  currentVersion?: string;
  /** electron-updater's UpdateInfo, forwarded as-is. */
  info?: unknown;
  /** electron-updater's ProgressInfo, forwarded as-is. */
  progress?: unknown;
}

export type UpdateSettings = Record<string, unknown>;

/** The parts of electron-updater's `autoUpdater` this module drives. */
export interface DesktopUpdateBackend {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  forceDevUpdateConfig: boolean;
  on: ((event: "checking-for-update" | "update-not-available", listener: () => void) => unknown) &
    ((
      event: "update-available" | "update-downloaded" | "download-progress",
      listener: (info: unknown) => void,
    ) => unknown) &
    ((event: "error", listener: (error: unknown) => void) => unknown);
  checkForUpdates: () => Promise<unknown>;
  downloadUpdate: () => Promise<unknown>;
  quitAndInstall: (isSilent: boolean, isForceRunAfter: boolean) => void;
}

/** A window that receives update status broadcasts. */
export interface UpdateStatusWindow {
  isDestroyed: () => boolean;
  webContents: { send: (channel: string, status: UpdateStatus) => void };
}

/** The parts of an IPC event the updater reads. */
export interface UpdaterIpcEvent {
  sender: unknown;
}

/** The consent dialog's options, as this module passes them. */
export interface UpdateConsentDialogOptions {
  type: "warning";
  icon: { isEmpty: () => boolean } | undefined;
  title: string;
  message: string;
  detail: string;
  buttons: string[];
  defaultId: number;
  cancelId: number;
  noLink: boolean;
}

export interface DesktopUpdaterDeps<
  Window extends UpdateStatusWindow,
  Event extends UpdaterIpcEvent,
> {
  app: { isPackaged: boolean; getVersion: () => string; quit: () => void };
  BrowserWindow: {
    getAllWindows: () => Window[];
    fromWebContents: (webContents: Event["sender"]) => Window | null;
  };
  ipcMain: {
    handle: (channel: string, listener: (event: Event, ...args: unknown[]) => unknown) => void;
  };
  dialog: {
    showMessageBox: (
      window: Window,
      options: UpdateConsentDialogOptions,
    ) => Promise<{ response: number }>;
  };
  /** The `nativeImage` module. */
  nativeImage: { createFromPath: (path: string) => { isEmpty: () => boolean } };
  autoUpdater: DesktopUpdateBackend;
  loadSettings: () => UpdateSettings;
  saveSettings: (settings: UpdateSettings) => void;
  /** Whether an IPC call came from a connected server's own pinned page. */
  isPinnedOriginSender: (event: Event) => boolean;
  /** The origin a window is pinned to (used for the consent dialog copy). */
  pinnedOrigin: (win: Window) => string | null;
  /** Absolute path to the app icon PNG. */
  iconPath: string;
  /**
   * Enable the development update config in an unpackaged build (main.js sets
   * this from !app.isPackaged).
   */
  forceDevUpdateConfig?: boolean;
  /** Disable the production feed for dev packages. */
  updatesEnabled?: boolean;
  /** Version shown in update UI. Defaults to Electron's real app version. */
  getCurrentVersion?: (() => string) | undefined;
  /** Called when a downloaded update becomes ready or stops being ready. */
  onInstallReadyChange?: (installReady: boolean) => void;
}

export interface DesktopUpdater {
  getConfig: () => UpdateConfig;
  /** `patch` comes from the renderer; unknown fields are ignored. */
  setConfig: (patch?: unknown) => UpdateConfig;
  getStatus: () => UpdateStatus;
  init: () => void;
  checkForUpdates: (options?: { manual?: boolean }) => Promise<void>;
  downloadUpdate: () => Promise<void>;
  installUpdateNow: () => boolean;
  registerIpc: () => void;
  quitAndInstallIfPending: () => boolean;
  readonly installPending: boolean;
}

type ConsentAction = "download" | "install" | "config";

function isUpdateMode(value: unknown): value is UpdateMode {
  return (UPDATE_MODES as ReadonlySet<unknown>).has(value);
}

/** An error's message, or the thrown value itself as text. */
function errorMessage(error: unknown): string {
  const message =
    typeof error === "object" && error !== null && "message" in error ? error.message : undefined;
  return String(message ?? error);
}

/**
 * Normalize the raw settings object into the update config the renderer sees.
 * Unknown/missing values fall back to the safe defaults (auto-checking on,
 * auto-install on, nothing skipped).
 */
export function normalizeUpdateConfig(settings: UpdateSettings): UpdateConfig {
  const mode = isUpdateMode(settings.update_mode) ? settings.update_mode : "default";
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
 */
export function isUpdateSecurityError(message: string): boolean {
  return /signature|sha512|checksum|not signed|code sign/i.test(message);
}

/**
 * Build the desktop updater. All host dependencies are injected so the module
 * carries no Electron/`main.js` globals and is directly unit-testable with
 * fakes.
 */
export function createDesktopUpdater<
  Window extends UpdateStatusWindow,
  Event extends UpdaterIpcEvent,
>({
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
  updatesEnabled = true,
  getCurrentVersion = () => app.getVersion(),
  onInstallReadyChange = () => {},
}: DesktopUpdaterDeps<Window, Event>): DesktopUpdater {
  let updateCheckTimer: ReturnType<typeof setInterval> | null = null;
  let currentUpdateStatus: UpdateStatus = { state: "idle" };
  let installPending = false;
  // A user-initiated ("Check for Updates…") check in flight — lets the error
  // handler surface a failure to the user only when they explicitly asked.
  let manualCheckInFlight = false;
  let statusBeforeCheck: UpdateStatus | null = null;

  function getConfig(): UpdateConfig {
    return normalizeUpdateConfig(loadSettings());
  }

  function setConfig(patch: unknown = {}): UpdateConfig {
    // Object.hasOwn throws on a null patch and finds no own keys on a primitive.
    const fields = patch as Record<string, unknown>;
    const settings = loadSettings();
    const next = { ...settings };
    if (Object.hasOwn(fields, "mode") && isUpdateMode(fields.mode)) {
      next.update_mode = fields.mode;
    }
    if (Object.hasOwn(fields, "autoInstall") && typeof fields.autoInstall === "boolean") {
      next.update_auto_install = fields.autoInstall;
    }
    if (Object.hasOwn(fields, "skippedVersion")) {
      next.update_skipped_version =
        typeof fields.skippedVersion === "string" ? fields.skippedVersion : null;
    }
    saveSettings(next);
    return getConfig();
  }

  function getStatus(): UpdateStatus {
    return currentUpdateStatus;
  }

  function broadcast(status: UpdateStatus): void {
    const wasInstallReady = currentUpdateStatus.state === "downloaded";
    currentUpdateStatus = status;
    const installReady = status.state === "downloaded";
    if (installReady !== wasInstallReady) onInstallReadyChange(installReady);
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
    return updatesEnabled && (app.isPackaged || autoUpdater.forceDevUpdateConfig === true);
  }

  function reportUnavailableInDev() {
    broadcast({ state: "idle", lastError: UPDATES_UNAVAILABLE_IN_DEV });
  }

  function unavailableInDevError() {
    return new Error(UPDATES_UNAVAILABLE_IN_DEV);
  }

  function applyConfig(cfg: UpdateConfig): void {
    if (updateCheckTimer) {
      clearInterval(updateCheckTimer);
      updateCheckTimer = null;
    }
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = cfg.autoInstall;
    // v1 is stable-only and rides electron-updater's default "latest" channel.
    if (!canUseFeed() || cfg.mode === "none") return;
    if (cfg.mode === "manual") return;
    checkForUpdates().catch(() => {});
    if (cfg.mode === "default") {
      updateCheckTimer = setInterval(
        () => checkForUpdates().catch(() => {}),
        PERIODIC_CHECK_INTERVAL_MS,
      );
      // Don't let the 6-hourly re-check keep the Node event loop alive at quit
      // (a ref'd interval can leave the main process lingering after the
      // windows close on some platforms).
      if (typeof updateCheckTimer.unref === "function") updateCheckTimer.unref();
    }
  }

  function init() {
    const cfg = getConfig();
    if (forceDevUpdateConfig) {
      autoUpdater.forceDevUpdateConfig = true;
    }
    autoUpdater.on("checking-for-update", () => {
      if (
        currentUpdateStatus.state !== "downloading" &&
        currentUpdateStatus.state !== "downloaded"
      ) {
        broadcast({ state: "checking" });
      }
    });
    autoUpdater.on("update-available", (info) => {
      manualCheckInFlight = false;
      if (
        currentUpdateStatus.state !== "downloading" &&
        currentUpdateStatus.state !== "downloaded"
      ) {
        broadcast({ state: "available", currentVersion: getCurrentVersion(), info });
      }
    });
    autoUpdater.on("update-not-available", () => {
      manualCheckInFlight = false;
      if (
        currentUpdateStatus.state !== "downloading" &&
        currentUpdateStatus.state !== "downloaded"
      ) {
        broadcast({ state: "none" });
      }
    });
    autoUpdater.on("download-progress", (progress) =>
      broadcast({ state: "downloading", progress }),
    );
    autoUpdater.on("update-downloaded", (info) =>
      broadcast({ state: "downloaded", currentVersion: getCurrentVersion(), info }),
    );
    autoUpdater.on("error", (err) => {
      const msg = errorMessage(err);
      const isSecurity = isUpdateSecurityError(msg);
      const wasManualCheck = manualCheckInFlight;
      manualCheckInFlight = false;
      console[isSecurity ? "error" : "warn"]("[omnigent] update error:", msg);
      if (isSecurity || wasManualCheck) {
        broadcast({ state: isSecurity ? "error-security" : "idle", lastError: msg });
      } else if (currentUpdateStatus.state === "checking") {
        broadcast(statusBeforeCheck ?? { state: "idle" });
      }
    });
    applyConfig(cfg);
  }

  function checkForUpdates({ manual = false }: { manual?: boolean } = {}): Promise<void> {
    if (currentUpdateStatus.state === "downloading" || currentUpdateStatus.state === "downloaded") {
      return Promise.resolve();
    }
    if (!canUseFeed()) {
      if (manual) reportUnavailableInDev();
      return Promise.reject(unavailableInDevError());
    }
    statusBeforeCheck = currentUpdateStatus;
    if (manual) manualCheckInFlight = true;
    return autoUpdater
      .checkForUpdates()
      .then(() => {
        if (manual) manualCheckInFlight = false;
        statusBeforeCheck = null;
        return undefined;
      })
      .catch((err: unknown) => {
        const msg = errorMessage(err);
        if (manualCheckInFlight) {
          manualCheckInFlight = false;
          broadcast({
            state: isUpdateSecurityError(msg) ? "error-security" : "idle",
            lastError: msg,
          });
        } else if (!manual && currentUpdateStatus.state === "checking") {
          broadcast(statusBeforeCheck ?? { state: "idle" });
        }
        statusBeforeCheck = null;
        throw err;
      });
  }

  function installUpdateNow(): boolean {
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
   * Start downloading the available update. Feed-gated like the other actions.
   * Used by the trusted shell update overlay (the server-page IPC calls
   * `autoUpdater.downloadUpdate()` inline behind its own consent dialog).
   */
  function downloadUpdate(): Promise<void> {
    if (!canUseFeed()) {
      reportUnavailableInDev();
      return Promise.reject(unavailableInDevError());
    }
    // Enter the progress state immediately instead of waiting for the first
    // provider event. Fast downloads and slow-loading About windows then still
    // get a truthful starting state from the cached updater snapshot.
    broadcast({ state: "downloading", progress: { percent: 0 } });
    return autoUpdater
      .downloadUpdate()
      .then(() => undefined)
      .catch((err: unknown) => {
        const message = errorMessage(err);
        broadcast({
          state: isUpdateSecurityError(message) ? "error-security" : "idle",
          lastError: message,
        });
        throw err;
      });
  }

  /**
   * Native, per-action consent for a privileged update control. The IPC gate
   * only proves the call came FROM the pinned server's page — not that the USER
   * asked for it — so download/install/config changes re-confirm here. Returns
   * true only when the user picks "Allow Once".
   */
  async function confirmControl(win: Window | null, action: ConsentAction): Promise<boolean> {
    if (!win) return false;
    const pinned = pinnedOrigin(win);
    if (!pinned) return false;

    let host = pinned;
    try {
      host = new URL(pinned).host;
    } catch {
      // Keep the full origin string if it somehow doesn't parse.
    }

    const consentCopy: Record<ConsentAction, { message: string; detail: string } | undefined> = {
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
    };
    const copy = consentCopy[action];
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
      await downloadUpdate();
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
   * @returns Whether a pending install was handed off.
   */
  function quitAndInstallIfPending(): boolean {
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
    downloadUpdate,
    installUpdateNow,
    registerIpc,
    quitAndInstallIfPending,
    get installPending() {
      return installPending;
    },
  };
}
